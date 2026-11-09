#include "nw_graph.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <initializer_list>
#include <set>
#include <sstream>

namespace nw::graph {
namespace {

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

std::string Join(const std::vector<std::string>& parts,
                 std::string_view separator) {
    std::string out;
    for (std::size_t i = 0; i < parts.size(); ++i) {
        if (i != 0) {
            out.append(separator.data(), separator.size());
        }
        out.append(parts[i]);
    }
    return out;
}

bool IsHexDigit(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
           (c >= 'A' && c <= 'F');
}

std::string Lower(std::string_view text) {
    std::string out;
    out.reserve(text.size());
    for (char c : text) {
        out.push_back(static_cast<char>(
            std::tolower(static_cast<unsigned char>(c))));
    }
    return out;
}

bool FromCharsU32(std::string_view text, std::uint32_t& value) {
    if (text.empty()) {
        return false;
    }
    std::uint64_t result = 0;
    for (char c : text) {
        if (c < '0' || c > '9') {
            return false;
        }
        result = result * 10u + static_cast<std::uint64_t>(c - '0');
        if (result > 0xffffffffull) {
            return false;
        }
    }
    value = static_cast<std::uint32_t>(result);
    return true;
}

bool FromCharsU64(std::string_view text, std::uint64_t& value) {
    if (text.empty()) {
        return false;
    }
    std::uint64_t result = 0;
    for (char c : text) {
        if (c < '0' || c > '9') {
            return false;
        }
        const std::uint64_t digit = static_cast<std::uint64_t>(c - '0');
        if (result > (std::numeric_limits<std::uint64_t>::max() - digit) / 10u) {
            return false;
        }
        result = result * 10u + digit;
    }
    value = result;
    return true;
}

bool FromCharsI64(std::string_view text, std::int64_t& value) {
    if (text.empty()) {
        return false;
    }
    bool negative = false;
    std::size_t index = 0;
    if (text[index] == '-' || text[index] == '+') {
        negative = text[index] == '-';
        ++index;
        if (index == text.size()) {
            return false;
        }
    }
    std::uint64_t magnitude = 0;
    for (; index < text.size(); ++index) {
        const char c = text[index];
        if (c < '0' || c > '9') {
            return false;
        }
        const std::uint64_t digit = static_cast<std::uint64_t>(c - '0');
        if (magnitude > (std::numeric_limits<std::uint64_t>::max() - digit) / 10u) {
            return false;
        }
        magnitude = magnitude * 10u + digit;
    }
    if (negative) {
        if (magnitude > static_cast<std::uint64_t>(
                            std::numeric_limits<std::int64_t>::max()) +
                            1ull) {
            return false;
        }
        if (magnitude == static_cast<std::uint64_t>(
                             std::numeric_limits<std::int64_t>::max()) +
                             1ull) {
            value = std::numeric_limits<std::int64_t>::min();
        } else {
            value = -static_cast<std::int64_t>(magnitude);
        }
    } else {
        if (magnitude > static_cast<std::uint64_t>(
                            std::numeric_limits<std::int64_t>::max())) {
            return false;
        }
        value = static_cast<std::int64_t>(magnitude);
    }
    return true;
}

bool ParseDouble(std::string_view text, double& value) {
    if (text.empty()) {
        return false;
    }
    std::string copy(text.data(), text.size());
    char* end = nullptr;
    const double parsed = std::strtod(copy.c_str(), &end);
    if (end == copy.c_str() || end == nullptr ||
        static_cast<std::size_t>(end - copy.c_str()) != copy.size()) {
        return false;
    }
    value = parsed;
    return true;
}

double NormalizeZero(double value) {
    return value == 0.0 ? 0.0 : value;
}

std::string HexBytes(const std::uint8_t* data, std::size_t size) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string out;
    out.reserve(size * 2u);
    for (std::size_t i = 0; i < size; ++i) {
        out.push_back(kHex[(data[i] >> 4u) & 0x0fu]);
        out.push_back(kHex[data[i] & 0x0fu]);
    }
    return out;
}

std::string Sha256HexBytes(std::string_view bytes) {
    const auto digest = Sha256Bytes(
        reinterpret_cast<const std::uint8_t*>(bytes.data()), bytes.size());
    return HexBytes(digest.data(), digest.size());
}

bool IsSemanticFloatFinite(double value) {
    return std::isfinite(value);
}

bool AnyComponentNonFinite(const ParamValue& value) {
    if (value.type == ParamType::Float) {
        return !IsSemanticFloatFinite(value.number);
    }
    if (value.type == ParamType::Vec2 || value.type == ParamType::Vec3 ||
        value.type == ParamType::Vec4 || value.type == ParamType::Color ||
        value.type == ParamType::Mat4) {
        for (std::uint32_t i = 0; i < value.component_count; ++i) {
            if (!IsSemanticFloatFinite(value.components[i])) {
                return true;
            }
        }
    }
    return false;
}

bool IsWellFormedProfileId(std::string_view id) {
    if (id.size() < 6u || id.substr(0, 3) != "nw.") {
        return false;
    }
    if (id.find('.') == std::string_view::npos) {
        return false;
    }
    const std::size_t version_pos = id.rfind(".v");
    if (version_pos == std::string_view::npos ||
        version_pos + 2u >= id.size()) {
        return false;
    }
    for (std::size_t i = version_pos + 2u; i < id.size(); ++i) {
        if (id[i] < '0' || id[i] > '9') {
            return false;
        }
    }
    std::size_t dots = 0;
    for (char c : id) {
        if (c == '.') {
            ++dots;
        }
    }
    return dots >= 4u;
}

bool IsWellFormedDigest(std::string_view digest) {
    constexpr std::string_view prefix = "sha256:v1:";
    if (digest.substr(0, prefix.size()) != prefix) {
        return false;
    }
    if (digest.size() != prefix.size() + 64u) {
        return false;
    }
    for (std::size_t i = prefix.size(); i < digest.size(); ++i) {
        if (!IsHexDigit(digest[i])) {
            return false;
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// SHA-256
// ---------------------------------------------------------------------------

inline std::uint32_t Rotr(std::uint32_t value, std::uint32_t count) {
    return (value >> count) | (value << (32u - count));
}

} // namespace

std::array<std::uint8_t, 32> Sha256Bytes(const std::uint8_t* data,
                                         std::size_t size) {
    static constexpr std::uint32_t kConstants[64] = {
        0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu,
        0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u,
        0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u,
        0xc19bf174u, 0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
        0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau, 0x983e5152u,
        0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
        0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu,
        0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
        0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u,
        0xd6990624u, 0xf40e3585u, 0x106aa070u, 0x19a4c116u, 0x1e376c08u,
        0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu,
        0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
        0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
    };

    std::uint32_t h[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u,
                          0xa54ff53au, 0x510e527fu, 0x9b05688cu,
                          0x1f83d9abu, 0x5be0cd19u};

    std::vector<std::uint8_t> message;
    message.reserve(size + 72u);
    if (size != 0u) {
        message.insert(message.end(), data, data + size);
    }
    const std::uint64_t bit_length =
        static_cast<std::uint64_t>(size) * 8ull;
    message.push_back(0x80u);
    while ((message.size() % 64u) != 56u) {
        message.push_back(0u);
    }
    for (int i = 7; i >= 0; --i) {
        message.push_back(static_cast<std::uint8_t>(
            (bit_length >> (static_cast<unsigned>(i) * 8u)) & 0xffu));
    }

    for (std::size_t offset = 0; offset < message.size(); offset += 64u) {
        std::uint32_t w[64]{};
        for (std::size_t i = 0; i < 16u; ++i) {
            const std::size_t p = offset + i * 4u;
            w[i] = (static_cast<std::uint32_t>(message[p]) << 24u) |
                   (static_cast<std::uint32_t>(message[p + 1u]) << 16u) |
                   (static_cast<std::uint32_t>(message[p + 2u]) << 8u) |
                   static_cast<std::uint32_t>(message[p + 3u]);
        }
        for (std::size_t i = 16u; i < 64u; ++i) {
            const std::uint32_t s0 =
                Rotr(w[i - 15u], 7u) ^ Rotr(w[i - 15u], 18u) ^
                (w[i - 15u] >> 3u);
            const std::uint32_t s1 =
                Rotr(w[i - 2u], 17u) ^ Rotr(w[i - 2u], 19u) ^
                (w[i - 2u] >> 10u);
            w[i] = w[i - 16u] + s0 + w[i - 7u] + s1;
        }

        std::uint32_t a = h[0];
        std::uint32_t b = h[1];
        std::uint32_t c = h[2];
        std::uint32_t d = h[3];
        std::uint32_t e = h[4];
        std::uint32_t f = h[5];
        std::uint32_t g = h[6];
        std::uint32_t hh = h[7];
        for (std::size_t i = 0; i < 64u; ++i) {
            const std::uint32_t s1 =
                Rotr(e, 6u) ^ Rotr(e, 11u) ^ Rotr(e, 25u);
            const std::uint32_t ch = (e & f) ^ ((~e) & g);
            const std::uint32_t temp1 = hh + s1 + ch + kConstants[i] + w[i];
            const std::uint32_t s0 =
                Rotr(a, 2u) ^ Rotr(a, 13u) ^ Rotr(a, 22u);
            const std::uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            const std::uint32_t temp2 = s0 + maj;
            hh = g;
            g = f;
            f = e;
            e = d + temp1;
            d = c;
            c = b;
            b = a;
            a = temp1 + temp2;
        }
        h[0] += a;
        h[1] += b;
        h[2] += c;
        h[3] += d;
        h[4] += e;
        h[5] += f;
        h[6] += g;
        h[7] += hh;
    }

    std::array<std::uint8_t, 32> result{};
    for (std::size_t i = 0; i < 8u; ++i) {
        result[i * 4u] = static_cast<std::uint8_t>((h[i] >> 24u) & 0xffu);
        result[i * 4u + 1u] =
            static_cast<std::uint8_t>((h[i] >> 16u) & 0xffu);
        result[i * 4u + 2u] =
            static_cast<std::uint8_t>((h[i] >> 8u) & 0xffu);
        result[i * 4u + 3u] = static_cast<std::uint8_t>(h[i] & 0xffu);
    }
    return result;
}

std::string Sha256Hex(std::string_view bytes) {
    return Sha256HexBytes(bytes);
}

std::string Sha256Digest(std::string_view bytes) {
    return "sha256:v1:" + Sha256HexBytes(bytes);
}

namespace {

// ---------------------------------------------------------------------------
// Canonical field writer
// ---------------------------------------------------------------------------

class CanonicalWriter {
public:
    void Tag(std::uint8_t tag) { bytes_.push_back(tag); }

    void String(std::string_view value) {
        Tag(0x01u);
        const std::uint32_t size = static_cast<std::uint32_t>(value.size());
        WriteU32(size);
        bytes_.insert(bytes_.end(), value.begin(), value.end());
    }

    void U64(std::uint64_t value) {
        Tag(0x02u);
        for (int i = 7; i >= 0; --i) {
            bytes_.push_back(static_cast<std::uint8_t>(
                (value >> (static_cast<unsigned>(i) * 8u)) & 0xffu));
        }
    }

    void I64(std::int64_t value) {
        U64(static_cast<std::uint64_t>(value));
    }

    void F64(double value) {
        Tag(0x03u);
        if (!std::isfinite(value)) {
            // Validation rejects non-finite semantics before encoding. This
            // fallback keeps the encoder total without hashing trap patterns.
            value = 0.0;
        }
        value = NormalizeZero(value);
        std::uint64_t bits = 0;
        std::memcpy(&bits, &value, sizeof(bits));
        for (int i = 7; i >= 0; --i) {
            bytes_.push_back(static_cast<std::uint8_t>(
                (bits >> (static_cast<unsigned>(i) * 8u)) & 0xffu));
        }
    }

    void Bool(bool value) {
        Tag(0x04u);
        bytes_.push_back(value ? 1u : 0u);
    }

    void Version(const SemanticVersion& version) {
        Tag(0x05u);
        WriteU32(version.major);
        WriteU32(version.minor);
        WriteU32(version.patch);
    }

    void ListHeader(std::uint32_t count) {
        Tag(0x08u);
        WriteU32(count);
    }

    void MapHeader(std::uint32_t count) {
        Tag(0x09u);
        WriteU32(count);
    }

    const std::vector<std::uint8_t>& bytes() const { return bytes_; }
    std::vector<std::uint8_t> Take() { return std::move(bytes_); }

private:
    void WriteU32(std::uint32_t value) {
        bytes_.push_back(static_cast<std::uint8_t>((value >> 24u) & 0xffu));
        bytes_.push_back(static_cast<std::uint8_t>((value >> 16u) & 0xffu));
        bytes_.push_back(static_cast<std::uint8_t>((value >> 8u) & 0xffu));
        bytes_.push_back(static_cast<std::uint8_t>(value & 0xffu));
    }

    std::vector<std::uint8_t> bytes_;
};

void EncodeParam(CanonicalWriter& writer, const ParamValue& value) {
    writer.ListHeader(2u);
    writer.String(ToString(value.type));
    switch (value.type) {
    case ParamType::Float:
        writer.F64(value.number);
        break;
    case ParamType::Int:
        writer.I64(value.integer);
        break;
    case ParamType::Bool:
        writer.Bool(value.boolean);
        break;
    case ParamType::String:
    case ParamType::Enum:
    case ParamType::Version:
    case ParamType::Expression:
        writer.String(value.text);
        break;
    case ParamType::Vec2:
    case ParamType::Vec3:
    case ParamType::Vec4:
    case ParamType::Color:
    case ParamType::Mat4:
        writer.U64(value.component_count);
        for (std::uint32_t i = 0; i < value.component_count; ++i) {
            writer.F64(value.components[i]);
        }
        break;
    }
}

void EncodeQuantization(CanonicalWriter& writer,
                        const QuantizationPolicy& quantization) {
    writer.String(quantization.policy);
    writer.U64(quantization.bits);
    writer.F64(quantization.step);
}

void EncodeCompilerSettings(CanonicalWriter& writer,
                            const CompilerSettings& settings) {
    std::map<std::string, std::string> values;
    values["optimization"] = settings.optimization;
    values["deterministic"] = settings.deterministic ? "true" : "false";
    values["api"] = settings.api;
    values["feature_level"] = settings.feature_level;
    values["shader_model"] = settings.shader_model;
    values["frequency_policy"] = settings.frequency_policy;
    values["shader_blob_policy"] = settings.shader_blob_policy;
    writer.MapHeader(static_cast<std::uint32_t>(values.size()));
    for (const auto& [key, value] : values) {
        writer.String(key);
        writer.String(value);
    }
    // Quantization is written separately from the full-precision authoring
    // parameters, exactly as the authoring schema requires.
    EncodeQuantization(writer, settings.quantization);
}

void EncodeResourceDescriptor(CanonicalWriter& writer,
                              const ResourceDescriptor& descriptor) {
    writer.String(descriptor.kind);
    writer.U64(descriptor.max_width);
    writer.U64(descriptor.max_height);
    writer.U64(descriptor.max_depth);
    writer.U64(descriptor.max_bytes);
    writer.U64(descriptor.max_vertices);
    writer.U64(descriptor.max_indices);
    writer.String(descriptor.format_class);
    writer.Bool(descriptor.bounded);
}

void EncodePort(CanonicalWriter& writer, const PortSpec& port) {
    writer.String(port.id);
    writer.String(ToString(port.type));
    writer.String(ToString(port.residency));
    writer.U64(port.dimension);
    writer.Bool(port.descriptor.has_value());
    if (port.descriptor) {
        EncodeResourceDescriptor(writer, *port.descriptor);
    }
}

void EncodeParamSpec(CanonicalWriter& writer, const ParamSpec& spec) {
    writer.String(spec.id);
    writer.String(ToString(spec.type));
    writer.Bool(spec.required);
    writer.Bool(spec.default_value.has_value());
    if (spec.default_value) {
        EncodeParam(writer, *spec.default_value);
    }
    writer.String(ToString(spec.normalization));
    writer.Bool(spec.has_range);
    writer.F64(spec.min_value);
    writer.F64(spec.max_value);
    writer.Bool(spec.bounded_resource_dimension);
    writer.ListHeader(static_cast<std::uint32_t>(spec.enum_values.size()));
    for (const auto& value : spec.enum_values) {
        writer.String(value);
    }
}

void EncodeFrequencyPolicy(CanonicalWriter& writer,
                           const FrequencyPolicy& policy) {
    std::vector<Frequency> allowed = policy.allowed;
    std::sort(allowed.begin(), allowed.end(),
              [](Frequency a, Frequency b) {
                  return static_cast<int>(a) < static_cast<int>(b);
              });
    writer.ListHeader(static_cast<std::uint32_t>(allowed.size()));
    for (Frequency frequency : allowed) {
        writer.U64(static_cast<std::uint64_t>(frequency));
    }
    writer.U64(static_cast<std::uint64_t>(policy.default_frequency));
}

void EncodeSeedBehaviors(CanonicalWriter& writer,
                         const std::vector<SeedBehavior>& behaviors) {
    std::vector<SeedBehavior> sorted = behaviors;
    std::sort(sorted.begin(), sorted.end(),
              [](SeedBehavior a, SeedBehavior b) {
                  return static_cast<int>(a) < static_cast<int>(b);
              });
    writer.ListHeader(static_cast<std::uint32_t>(sorted.size()));
    for (SeedBehavior behavior : sorted) {
        writer.String(ToString(behavior));
    }
}

std::string RawDigestHex(const std::vector<std::uint8_t>& bytes) {
    const auto digest =
        Sha256Bytes(bytes.empty() ? nullptr : bytes.data(), bytes.size());
    return HexBytes(digest.data(), digest.size());
}

} // namespace

std::string OperatorContractDigest(const OperatorSchema& schema) {
    CanonicalWriter writer;
    writer.String("nwgraph.operator.v1");
    writer.String(schema.id);
    writer.Version(schema.version);
    writer.String(ToString(schema.kind));
    writer.String(schema.lowering_hook);
    writer.String(schema.lowering_hook_version);
    EncodeFrequencyPolicy(writer, schema.frequency_policy);
    EncodeSeedBehaviors(writer, schema.allowed_seed_behaviors);
    writer.String(ToString(schema.default_seed_behavior));
    writer.Bool(schema.produces_resource);
    writer.Bool(schema.explicit_readback);
    writer.Bool(schema.explicit_residency_conversion);
    writer.Bool(schema.resource_descriptor.has_value());
    if (schema.resource_descriptor) {
        EncodeResourceDescriptor(writer, *schema.resource_descriptor);
    }

    std::vector<PortSpec> inputs = schema.inputs;
    std::sort(inputs.begin(), inputs.end(),
              [](const PortSpec& a, const PortSpec& b) { return a.id < b.id; });
    writer.ListHeader(static_cast<std::uint32_t>(inputs.size()));
    for (const auto& port : inputs) {
        EncodePort(writer, port);
    }

    std::vector<PortSpec> outputs = schema.outputs;
    std::sort(outputs.begin(), outputs.end(),
              [](const PortSpec& a, const PortSpec& b) { return a.id < b.id; });
    writer.ListHeader(static_cast<std::uint32_t>(outputs.size()));
    for (const auto& port : outputs) {
        EncodePort(writer, port);
    }

    std::vector<ParamSpec> params = schema.params;
    std::sort(params.begin(), params.end(),
              [](const ParamSpec& a, const ParamSpec& b) { return a.id < b.id; });
    writer.ListHeader(static_cast<std::uint32_t>(params.size()));
    for (const auto& param : params) {
        EncodeParamSpec(writer, param);
    }

    writer.ListHeader(static_cast<std::uint32_t>(
        schema.shader_operations.size()));
    for (const auto& operation : schema.shader_operations) {
        writer.String(operation);
    }

    return "sha256:v1:" + RawDigestHex(writer.bytes());
}

// ---------------------------------------------------------------------------
// Vocabulary conversions
// ---------------------------------------------------------------------------

std::string_view ToString(LogicalType type) noexcept {
    switch (type) {
    case LogicalType::Scalar: return "scalar";
    case LogicalType::Vector: return "vector";
    case LogicalType::Matrix: return "matrix";
    case LogicalType::Curve: return "curve";
    case LogicalType::ShaderExpr: return "shader_expr";
    case LogicalType::TextureRecipe: return "texture_recipe";
    case LogicalType::BufferRecipe: return "buffer_recipe";
    case LogicalType::MeshRecipe: return "mesh_recipe";
    case LogicalType::Material: return "material";
    case LogicalType::SceneTable: return "scene_table";
    case LogicalType::Camera: return "camera";
    case LogicalType::Light: return "light";
    case LogicalType::RenderImage: return "render_image";
    }
    return "scalar";
}

std::optional<LogicalType> ParseLogicalType(std::string_view text) {
    static constexpr std::pair<std::string_view, LogicalType> kValues[] = {
        {"scalar", LogicalType::Scalar},
        {"vector", LogicalType::Vector},
        {"matrix", LogicalType::Matrix},
        {"curve", LogicalType::Curve},
        {"shader_expr", LogicalType::ShaderExpr},
        {"texture_recipe", LogicalType::TextureRecipe},
        {"buffer_recipe", LogicalType::BufferRecipe},
        {"mesh_recipe", LogicalType::MeshRecipe},
        {"material", LogicalType::Material},
        {"scene_table", LogicalType::SceneTable},
        {"camera", LogicalType::Camera},
        {"light", LogicalType::Light},
        {"render_image", LogicalType::RenderImage},
    };
    for (const auto& [name, value] : kValues) {
        if (name == text) {
            return value;
        }
    }
    return std::nullopt;
}

std::string_view ToString(Residency residency) noexcept {
    switch (residency) {
    case Residency::Logical: return "logical";
    case Residency::Cpu: return "cpu";
    case Residency::Gpu: return "gpu";
    case Residency::Readback: return "readback";
    }
    return "logical";
}

std::optional<Residency> ParseResidency(std::string_view text) {
    if (text == "logical") return Residency::Logical;
    if (text == "cpu") return Residency::Cpu;
    if (text == "gpu") return Residency::Gpu;
    if (text == "readback") return Residency::Readback;
    return std::nullopt;
}

std::string_view ToString(Frequency frequency) noexcept {
    switch (frequency) {
    case Frequency::CompileTime: return "compile-time";
    case Frequency::Initialization: return "initialization";
    case Frequency::Frame: return "frame";
    case Frequency::History: return "history";
    }
    return "frame";
}

std::optional<Frequency> ParseFrequency(std::string_view text) {
    if (text == "compile-time" || text == "compile") {
        return Frequency::CompileTime;
    }
    if (text == "initialization" || text == "init") {
        return Frequency::Initialization;
    }
    if (text == "frame") {
        return Frequency::Frame;
    }
    if (text == "history") {
        return Frequency::History;
    }
    return std::nullopt;
}

std::string_view ToString(OperatorKind kind) noexcept {
    switch (kind) {
    case OperatorKind::PureShaderExpr: return "pure_shader_expr";
    case OperatorKind::GpuPass: return "gpu_pass";
    case OperatorKind::CpuControl: return "cpu_control";
    }
    return "cpu_control";
}

std::string_view ToString(SeedBehavior behavior) noexcept {
    switch (behavior) {
    case SeedBehavior::None: return "none";
    case SeedBehavior::Explicit: return "explicit";
    case SeedBehavior::DerivedFromGraph: return "derived-graph";
    case SeedBehavior::DerivedFromUpstream: return "derived-upstream";
    case SeedBehavior::ExplicitNodeId: return "node-id";
    }
    return "none";
}

std::optional<SeedBehavior> ParseSeedBehavior(std::string_view text) {
    if (text == "none") return SeedBehavior::None;
    if (text == "explicit") return SeedBehavior::Explicit;
    if (text == "derived-graph" || text == "graph") {
        return SeedBehavior::DerivedFromGraph;
    }
    if (text == "derived-upstream" || text == "upstream") {
        return SeedBehavior::DerivedFromUpstream;
    }
    if (text == "node-id") return SeedBehavior::ExplicitNodeId;
    return std::nullopt;
}

std::string_view ToString(ParamType type) noexcept {
    switch (type) {
    case ParamType::Float: return "float";
    case ParamType::Int: return "int";
    case ParamType::Bool: return "bool";
    case ParamType::String: return "string";
    case ParamType::Enum: return "enum";
    case ParamType::Vec2: return "vec2";
    case ParamType::Vec3: return "vec3";
    case ParamType::Vec4: return "vec4";
    case ParamType::Color: return "color";
    case ParamType::Version: return "version";
    case ParamType::Expression: return "expression";
    case ParamType::Mat4: return "mat4";
    }
    return "string";
}

std::string_view ToString(NormalizationRule rule) noexcept {
    switch (rule) {
    case NormalizationRule::None: return "none";
    case NormalizationRule::FiniteOnly: return "finite-only";
    case NormalizationRule::Clamp01: return "clamp01";
    case NormalizationRule::NormalizeVector: return "normalize-vector";
    }
    return "finite-only";
}

std::string SemanticVersion::ToString() const {
    return std::to_string(major) + "." + std::to_string(minor) + "." +
           std::to_string(patch);
}

std::optional<SemanticVersion> SemanticVersion::Parse(std::string_view text) {
    std::uint32_t parts[3]{};
    std::size_t part_index = 0;
    std::size_t start = 0;
    for (std::size_t i = 0; i <= text.size(); ++i) {
        if (i == text.size() || text[i] == '.') {
            if (part_index >= 3u ||
                !FromCharsU32(text.substr(start, i - start),
                              parts[part_index])) {
                return std::nullopt;
            }
            ++part_index;
            start = i + 1u;
        }
    }
    if (part_index != 3u) {
        return std::nullopt;
    }
    return SemanticVersion{parts[0], parts[1], parts[2]};
}

bool operator==(const SemanticVersion& a, const SemanticVersion& b) noexcept {
    return a.major == b.major && a.minor == b.minor && a.patch == b.patch;
}

bool operator!=(const SemanticVersion& a, const SemanticVersion& b) noexcept {
    return !(a == b);
}

bool operator<(const SemanticVersion& a, const SemanticVersion& b) noexcept {
    if (a.major != b.major) return a.major < b.major;
    if (a.minor != b.minor) return a.minor < b.minor;
    return a.patch < b.patch;
}

const PortSpec* OperatorSchema::FindInput(std::string_view id) const {
    for (const auto& port : inputs) {
        if (std::string_view(port.id) == id) {
            return &port;
        }
    }
    return nullptr;
}

const PortSpec* OperatorSchema::FindOutput(std::string_view id) const {
    for (const auto& port : outputs) {
        if (std::string_view(port.id) == id) {
            return &port;
        }
    }
    return nullptr;
}

const ParamSpec* OperatorSchema::FindParam(std::string_view id) const {
    for (const auto& param : params) {
        if (std::string_view(param.id) == id) {
            return &param;
        }
    }
    return nullptr;
}

Residency OperatorSchema::PrimaryResidency() const {
    return outputs.empty() ? Residency::Cpu : outputs.front().residency;
}

bool OperatorRegistry::Register(OperatorSchema schema) {
    for (const auto& existing : operators_) {
        if (existing.id == schema.id && existing.version == schema.version) {
            return false;
        }
    }
    operators_.push_back(std::move(schema));
    return true;
}

const OperatorSchema* OperatorRegistry::Find(
    std::string_view id, const SemanticVersion& version) const {
    for (const auto& schema : operators_) {
        if (std::string_view(schema.id) == id &&
            schema.version == version) {
            return &schema;
        }
    }
    return nullptr;
}

const OperatorSchema* OperatorRegistry::FindAny(std::string_view id) const {
    for (const auto& schema : operators_) {
        if (std::string_view(schema.id) == id) {
            return &schema;
        }
    }
    return nullptr;
}

bool RegisterAuthoringExtension(OperatorRegistry& registry,
                                const ExtensionRegistration& extension,
                                std::vector<std::string>& diagnostics) {
    bool ok = true;
    for (const auto& schema : extension.operators) {
        if (schema.id.empty() || schema.version == SemanticVersion{}) {
            diagnostics.push_back("extension " + extension.extension_id +
                                  " contains an operator without a stable id/version");
            ok = false;
            continue;
        }
        if (!registry.Register(schema)) {
            diagnostics.push_back("duplicate operator registration: " +
                                  schema.id + "@" +
                                  schema.version.ToString());
            ok = false;
        }
    }
    return ok;
}

namespace {

PortSpec Port(std::string id, LogicalType type, Residency residency,
              std::uint32_t dimension = 0) {
    PortSpec port;
    port.id = std::move(id);
    port.type = type;
    port.residency = residency;
    port.dimension = dimension;
    return port;
}

PortSpec ResourcePort(std::string id, LogicalType type, Residency residency,
                      ResourceDescriptor descriptor,
                      std::uint32_t dimension = 0) {
    PortSpec port = Port(std::move(id), type, residency, dimension);
    port.descriptor = std::move(descriptor);
    return port;
}

ParamValue FloatValue(double value) {
    ParamValue param;
    param.type = ParamType::Float;
    param.number = NormalizeZero(value);
    return param;
}

ParamValue IntValue(std::int64_t value) {
    ParamValue param;
    param.type = ParamType::Int;
    param.integer = value;
    return param;
}

ParamSpec FloatParam(std::string id, double default_value,
                     bool has_range = false, double min_value = 0.0,
                     double max_value = 0.0,
                     NormalizationRule rule = NormalizationRule::FiniteOnly,
                     bool bounded_resource_dimension = false) {
    ParamSpec spec;
    spec.id = std::move(id);
    spec.type = ParamType::Float;
    spec.required = false;
    spec.default_value = FloatValue(default_value);
    spec.has_range = has_range;
    spec.min_value = min_value;
    spec.max_value = max_value;
    spec.normalization = rule;
    spec.bounded_resource_dimension = bounded_resource_dimension;
    return spec;
}

ParamSpec IntParam(std::string id, std::int64_t default_value,
                   bool has_range = false, double min_value = 0.0,
                   double max_value = 0.0,
                   bool bounded_resource_dimension = false) {
    ParamSpec spec;
    spec.id = std::move(id);
    spec.type = ParamType::Int;
    spec.required = false;
    spec.default_value = IntValue(default_value);
    spec.has_range = has_range;
    spec.min_value = min_value;
    spec.max_value = max_value;
    spec.bounded_resource_dimension = bounded_resource_dimension;
    return spec;
}

ParamSpec StringParam(std::string id, std::string default_value,
                      ParamType type = ParamType::String) {
    ParamSpec spec;
    spec.id = std::move(id);
    spec.type = type;
    spec.required = false;
    ParamValue value;
    value.type = type;
    value.text = std::move(default_value);
    spec.default_value = std::move(value);
    return spec;
}

ParamSpec EnumParam(std::string id, std::string default_value,
                    std::vector<std::string> values) {
    ParamSpec spec = StringParam(std::move(id), std::move(default_value),
                                 ParamType::Enum);
    spec.enum_values = std::move(values);
    return spec;
}

ParamSpec VectorParam(std::string id, ParamType type) {
    ParamSpec spec;
    spec.id = std::move(id);
    spec.type = type;
    spec.required = false;
    ParamValue value;
    value.type = type;
    value.component_count = type == ParamType::Vec2   ? 2u
                            : type == ParamType::Vec3 ? 3u
                            : (type == ParamType::Vec4 ||
                               type == ParamType::Color)
                                  ? 4u
                                                      : 16u;
    spec.default_value = std::move(value);
    spec.normalization = NormalizationRule::FiniteOnly;
    return spec;
}

OperatorSchema BaseOperator(std::string id, OperatorKind kind,
                            std::string summary,
                            std::vector<Frequency> frequencies,
                            Frequency default_frequency,
                            std::string lowering_hook) {
    OperatorSchema schema;
    schema.id = std::move(id);
    schema.version = SemanticVersion{1, 0, 0};
    schema.kind = kind;
    schema.summary = std::move(summary);
    schema.frequency_policy.allowed = std::move(frequencies);
    schema.frequency_policy.default_frequency = default_frequency;
    schema.default_seed_behavior = SeedBehavior::None;
    schema.allowed_seed_behaviors = {SeedBehavior::None};
    schema.lowering_hook = std::move(lowering_hook);
    schema.lowering_hook_version = "1.0.0";
    return schema;
}

ResourceDescriptor TextureDescriptor() {
    ResourceDescriptor descriptor;
    descriptor.kind = "texture2d";
    descriptor.max_width = 8192u;
    descriptor.max_height = 8192u;
    descriptor.max_bytes = 8192ull * 8192ull * 4ull;
    descriptor.format_class = "r8g8b8a8_unorm";
    descriptor.bounded = true;
    return descriptor;
}

ResourceDescriptor BufferDescriptor() {
    ResourceDescriptor descriptor;
    descriptor.kind = "buffer";
    descriptor.max_bytes = 64ull * 1024ull * 1024ull;
    descriptor.format_class = "r32_uint";
    descriptor.bounded = true;
    return descriptor;
}

ResourceDescriptor MeshDescriptor() {
    ResourceDescriptor descriptor;
    descriptor.kind = "mesh";
    descriptor.max_vertices = 1048576u;
    descriptor.max_indices = 3145728u;
    descriptor.format_class = "position_normal_uv";
    descriptor.bounded = true;
    return descriptor;
}

ResourceDescriptor RenderImageDescriptor() {
    ResourceDescriptor descriptor;
    descriptor.kind = "render_target";
    descriptor.max_width = 8192u;
    descriptor.max_height = 8192u;
    descriptor.max_bytes = 8192ull * 8192ull * 4ull;
    descriptor.format_class = "r8g8b8a8_unorm";
    descriptor.bounded = true;
    return descriptor;
}

const std::vector<Frequency> kAllFrequencies = {
    Frequency::CompileTime, Frequency::Initialization, Frequency::Frame};
const std::vector<Frequency> kRuntimeFrequencies = {
    Frequency::Initialization, Frequency::Frame};
const std::vector<Frequency> kFrameFrequency = {Frequency::Frame};

} // namespace

OperatorRegistry OperatorRegistry::CreateDefault() {
    OperatorRegistry registry;

    // ---- CPU control / math ------------------------------------------------
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.scalar_const", OperatorKind::CpuControl,
            "Full-precision scalar authoring constant", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.math");
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        schema.params = {FloatParam("value", 0.0)};
        schema.allowed_seed_behaviors = {SeedBehavior::None,
                                         SeedBehavior::Explicit};
        registry.Register(std::move(schema));
    }
    for (const auto& [id, dimension] :
         std::vector<std::pair<std::string, std::uint32_t>>{
             {"nw.math.vec2_const", 2u},
             {"nw.math.vec3_const", 3u},
             {"nw.math.vec4_const", 4u}}) {
        OperatorSchema schema = BaseOperator(
            id, OperatorKind::CpuControl,
            "Full-precision vector authoring constant", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.math");
        schema.outputs = {
            Port("out", LogicalType::Vector, Residency::Cpu, dimension)};
        schema.params = {VectorParam(
            "value", dimension == 2u   ? ParamType::Vec2
                     : dimension == 3u ? ParamType::Vec3
                                       : ParamType::Vec4)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.mat4_const", OperatorKind::CpuControl,
            "Full-precision matrix authoring constant", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.math");
        schema.outputs = {
            Port("out", LogicalType::Matrix, Residency::Cpu, 4u)};
        schema.params = {VectorParam("value", ParamType::Mat4)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.add", OperatorKind::CpuControl, "Scalar addition",
            kAllFrequencies, Frequency::Frame, "nw.lower.cpu.math");
        schema.inputs = {Port("a", LogicalType::Scalar, Residency::Cpu),
                         Port("b", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.mul", OperatorKind::CpuControl, "Scalar multiplication",
            kAllFrequencies, Frequency::Frame, "nw.lower.cpu.math");
        schema.inputs = {Port("a", LogicalType::Scalar, Residency::Cpu),
                         Port("b", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.sin", OperatorKind::CpuControl, "Scalar sine",
            kAllFrequencies, Frequency::Frame, "nw.lower.cpu.math");
        schema.inputs = {Port("x", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.clamp01", OperatorKind::CpuControl,
            "Clamp scalar to the unit interval", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.math");
        schema.inputs = {Port("x", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.vec4_scale", OperatorKind::CpuControl,
            "Scale a vec4 by a scalar", kAllFrequencies, Frequency::Frame,
            "nw.lower.cpu.math");
        schema.inputs = {Port("v", LogicalType::Vector, Residency::Cpu, 4u),
                         Port("s", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {
            Port("out", LogicalType::Vector, Residency::Cpu, 4u)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.vec4_split", OperatorKind::CpuControl,
            "Split a vec4 into four scalar outputs", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.math");
        schema.inputs = {Port("v", LogicalType::Vector, Residency::Cpu, 4u)};
        schema.outputs = {
            Port("x", LogicalType::Scalar, Residency::Cpu),
            Port("y", LogicalType::Scalar, Residency::Cpu),
            Port("z", LogicalType::Scalar, Residency::Cpu),
            Port("w", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.math.dot", OperatorKind::CpuControl, "Vec3 dot product",
            kAllFrequencies, Frequency::Frame, "nw.lower.cpu.math");
        schema.inputs = {Port("a", LogicalType::Vector, Residency::Cpu, 3u),
                         Port("b", LogicalType::Vector, Residency::Cpu, 3u)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.control.smoothstep", OperatorKind::CpuControl,
            "Deterministic scalar smoothstep", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.control");
        schema.inputs = {Port("edge0", LogicalType::Scalar, Residency::Cpu),
                         Port("edge1", LogicalType::Scalar, Residency::Cpu),
                         Port("x", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.control.seed", OperatorKind::CpuControl,
            "Explicit or graph-derived deterministic seed", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.control");
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        schema.default_seed_behavior = SeedBehavior::DerivedFromGraph;
        schema.allowed_seed_behaviors = {
            SeedBehavior::Explicit, SeedBehavior::DerivedFromGraph,
            SeedBehavior::DerivedFromUpstream, SeedBehavior::ExplicitNodeId};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.control.image_cpu_passthrough", OperatorKind::CpuControl,
            "Explicit CPU-resident render-image passthrough", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.control");
        schema.inputs = {Port("image", LogicalType::RenderImage,
                              Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::RenderImage,
                               Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.proc.hash_noise", OperatorKind::CpuControl,
            "Deterministic hash-noise procedural oracle", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.procedural");
        schema.inputs = {Port("seed", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        schema.default_seed_behavior = SeedBehavior::DerivedFromGraph;
        schema.allowed_seed_behaviors = {
            SeedBehavior::Explicit, SeedBehavior::DerivedFromGraph,
            SeedBehavior::DerivedFromUpstream};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.proc.value_noise", OperatorKind::CpuControl,
            "Deterministic value-noise procedural oracle", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.procedural");
        schema.inputs = {
            Port("position", LogicalType::Vector, Residency::Cpu, 3u)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.proc.checker", OperatorKind::CpuControl,
            "Deterministic checker procedural oracle", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.procedural");
        schema.inputs = {
            Port("uv", LogicalType::Vector, Residency::Cpu, 2u),
            Port("scale", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.proc.palette", OperatorKind::CpuControl,
            "Deterministic cosine palette procedural oracle", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.procedural");
        schema.inputs = {Port("t", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {
            Port("out", LogicalType::Vector, Residency::Cpu, 3u)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.curve.keyframes", OperatorKind::CpuControl,
            "Canonical keyframe curve authoring value", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.curve");
        schema.outputs = {Port("out", LogicalType::Curve, Residency::Cpu)};
        schema.params = {
            StringParam("keys", "0:0;1:1"),
            EnumParam("interpolation", "linear", {"linear", "step"})};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.curve.sample", OperatorKind::CpuControl,
            "Sample a canonical curve at a scalar time", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.curve");
        schema.inputs = {Port("curve", LogicalType::Curve, Residency::Cpu),
                         Port("t", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Scalar, Residency::Cpu)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.scene.flat", OperatorKind::CpuControl,
            "Flat scene table authoring value", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.scene");
        schema.outputs = {
            Port("out", LogicalType::SceneTable, Residency::Cpu)};
        schema.params = {StringParam("entries", "")};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.camera.perspective", OperatorKind::CpuControl,
            "Perspective camera authoring value", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.camera");
        schema.outputs = {Port("out", LogicalType::Camera, Residency::Cpu)};
        schema.params = {
            FloatParam("fov_degrees", 60.0, true, 1.0, 179.0,
                       NormalizationRule::FiniteOnly),
            FloatParam("aspect", 16.0 / 9.0, true, 0.0001, 1000.0),
            FloatParam("near", 0.1, true, 0.000001, 1000000.0),
            FloatParam("far", 1000.0, true, 0.000001, 1000000000.0)};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.light.directional", OperatorKind::CpuControl,
            "Directional light authoring value", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.light");
        schema.outputs = {Port("out", LogicalType::Light, Residency::Cpu)};
        ParamSpec direction = VectorParam("direction", ParamType::Vec3);
        direction.normalization = NormalizationRule::NormalizeVector;
        if (direction.default_value) {
            direction.default_value->components[1] = -1.0;
        }
        ParamSpec color = VectorParam("color", ParamType::Vec3);
        schema.params = {direction, color};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.material.describe", OperatorKind::CpuControl,
            "Material description authoring value", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.material");
        schema.inputs = {
            Port("base_color", LogicalType::Vector, Residency::Cpu, 4u),
            Port("roughness", LogicalType::Scalar, Residency::Cpu)};
        schema.outputs = {Port("out", LogicalType::Material, Residency::Cpu)};
        schema.params = {
            FloatParam("metallic", 0.0, true, 0.0, 1.0,
                       NormalizationRule::Clamp01),
            FloatParam("specular", 0.5, true, 0.0, 1.0,
                       NormalizationRule::Clamp01)};
        registry.Register(std::move(schema));
    }

    // ---- Pure ShaderExpr composition --------------------------------------
    {
        OperatorSchema schema = BaseOperator(
            "nw.shader.vec4_const", OperatorKind::PureShaderExpr,
            "Shader expression vec4 constant", kAllFrequencies,
            Frequency::Frame, "nw.lower.shader.expr");
        schema.outputs = {
            Port("out", LogicalType::ShaderExpr, Residency::Logical)};
        schema.params = {VectorParam("value", ParamType::Vec4)};
        schema.shader_operations = {"vec4", "const"};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.shader.expr", OperatorKind::PureShaderExpr,
            "Supported-operation shader expression", kAllFrequencies,
            Frequency::Frame, "nw.lower.shader.expr");
        schema.outputs = {
            Port("out", LogicalType::ShaderExpr, Residency::Logical)};
        ParamSpec expression =
            StringParam("expression", "vec4(uv.x, uv.y, 0.0, 1.0)",
                        ParamType::Expression);
        schema.params = {expression};
        schema.shader_operations = {"vec2", "vec3", "vec4", "sin", "cos",
                                    "mix", "texture2d", "uv", "time", "const"};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.shader.add", OperatorKind::PureShaderExpr,
            "Pure ShaderExpr addition", kAllFrequencies, Frequency::Frame,
            "nw.lower.shader.expr");
        schema.inputs = {
            Port("a", LogicalType::ShaderExpr, Residency::Logical),
            Port("b", LogicalType::ShaderExpr, Residency::Logical)};
        schema.outputs = {
            Port("out", LogicalType::ShaderExpr, Residency::Logical)};
        schema.shader_operations = {"add"};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.shader.mul", OperatorKind::PureShaderExpr,
            "Pure ShaderExpr multiplication", kAllFrequencies, Frequency::Frame,
            "nw.lower.shader.expr");
        schema.inputs = {
            Port("a", LogicalType::ShaderExpr, Residency::Logical),
            Port("b", LogicalType::ShaderExpr, Residency::Logical)};
        schema.outputs = {
            Port("out", LogicalType::ShaderExpr, Residency::Logical)};
        schema.shader_operations = {"mul"};
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.shader.mix", OperatorKind::PureShaderExpr,
            "Pure ShaderExpr mix", kAllFrequencies, Frequency::Frame,
            "nw.lower.shader.expr");
        schema.inputs = {
            Port("a", LogicalType::ShaderExpr, Residency::Logical),
            Port("b", LogicalType::ShaderExpr, Residency::Logical)};
        schema.outputs = {
            Port("out", LogicalType::ShaderExpr, Residency::Logical)};
        schema.params = {FloatParam("factor", 0.5, true, 0.0, 1.0,
                                    NormalizationRule::Clamp01)};
        schema.shader_operations = {"mix"};
        registry.Register(std::move(schema));
    }

    // ---- Resource recipes and GpuPass operations ---------------------------
    {
        OperatorSchema schema = BaseOperator(
            "nw.texture.procedural", OperatorKind::CpuControl,
            "Bounded procedural texture recipe", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.recipe");
        schema.outputs = {ResourcePort(
            "out", LogicalType::TextureRecipe, Residency::Logical,
            TextureDescriptor())};
        schema.params = {
            IntParam("width", 256, true, 1.0, 8192.0, true),
            IntParam("height", 256, true, 1.0, 8192.0, true),
            EnumParam("format", "r8g8b8a8_unorm",
                      {"r8g8b8a8_unorm", "rgba32f"}),
            IntParam("seed", 0, true, 0.0, 4294967295.0)};
        schema.resource_descriptor = TextureDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.buffer.recipe", OperatorKind::CpuControl,
            "Bounded buffer recipe", kAllFrequencies, Frequency::Frame,
            "nw.lower.cpu.recipe");
        schema.outputs = {ResourcePort(
            "out", LogicalType::BufferRecipe, Residency::Logical,
            BufferDescriptor())};
        schema.params = {
            IntParam("bytes", 4096, true, 1.0, 67108864.0, true),
            EnumParam("format", "r32_uint", {"r32_uint", "rgba32f"})};
        schema.resource_descriptor = BufferDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.mesh.recipe", OperatorKind::CpuControl,
            "Bounded procedural mesh recipe", kAllFrequencies,
            Frequency::Frame, "nw.lower.cpu.recipe");
        schema.outputs = {ResourcePort(
            "out", LogicalType::MeshRecipe, Residency::Logical,
            MeshDescriptor())};
        schema.params = {
            IntParam("vertices", 1024, true, 1.0, 1048576.0, true),
            IntParam("indices", 3072, true, 1.0, 3145728.0, true)};
        schema.resource_descriptor = MeshDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.residency.upload_texture", OperatorKind::GpuPass,
            "Explicit texture recipe upload", kRuntimeFrequencies,
            Frequency::Frame, "nw.lower.hlsl.residency");
        schema.inputs = {ResourcePort(
            "texture", LogicalType::TextureRecipe, Residency::Logical,
            TextureDescriptor())};
        schema.outputs = {ResourcePort(
            "out", LogicalType::TextureRecipe, Residency::Gpu,
            TextureDescriptor())};
        schema.explicit_residency_conversion = true;
        schema.produces_resource = true;
        schema.resource_descriptor = TextureDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.residency.upload_buffer", OperatorKind::GpuPass,
            "Explicit buffer recipe upload", kRuntimeFrequencies,
            Frequency::Frame, "nw.lower.hlsl.residency");
        schema.inputs = {ResourcePort(
            "buffer", LogicalType::BufferRecipe, Residency::Logical,
            BufferDescriptor())};
        schema.outputs = {ResourcePort(
            "out", LogicalType::BufferRecipe, Residency::Gpu,
            BufferDescriptor())};
        schema.explicit_residency_conversion = true;
        schema.produces_resource = true;
        schema.resource_descriptor = BufferDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.residency.upload_mesh", OperatorKind::GpuPass,
            "Explicit mesh recipe upload", kRuntimeFrequencies,
            Frequency::Frame, "nw.lower.hlsl.residency");
        schema.inputs = {ResourcePort(
            "mesh", LogicalType::MeshRecipe, Residency::Logical,
            MeshDescriptor())};
        schema.outputs = {ResourcePort(
            "out", LogicalType::MeshRecipe, Residency::Gpu,
            MeshDescriptor())};
        schema.explicit_residency_conversion = true;
        schema.produces_resource = true;
        schema.resource_descriptor = MeshDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.residency.gpu_to_cpu", OperatorKind::GpuPass,
            "Explicit render-image readback", {Frequency::Frame},
            Frequency::Frame, "nw.lower.hlsl.readback");
        schema.inputs = {
            ResourcePort("image", LogicalType::RenderImage, Residency::Gpu,
                         RenderImageDescriptor())};
        schema.outputs = {
            ResourcePort("out", LogicalType::RenderImage, Residency::Cpu,
                         RenderImageDescriptor())};
        schema.explicit_readback = true;
        schema.explicit_residency_conversion = true;
        schema.resource_descriptor = RenderImageDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.residency.cpu_to_gpu", OperatorKind::GpuPass,
            "Explicit render-image upload", kRuntimeFrequencies,
            Frequency::Frame, "nw.lower.hlsl.residency");
        schema.inputs = {
            ResourcePort("image", LogicalType::RenderImage, Residency::Cpu,
                         RenderImageDescriptor())};
        schema.outputs = {
            ResourcePort("out", LogicalType::RenderImage, Residency::Gpu,
                         RenderImageDescriptor())};
        schema.explicit_residency_conversion = true;
        schema.produces_resource = true;
        schema.resource_descriptor = RenderImageDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.readback.scalar", OperatorKind::GpuPass,
            "Explicit scalar readback from a render image",
            {Frequency::Frame}, Frequency::Frame, "nw.lower.hlsl.readback");
        schema.inputs = {
            ResourcePort("image", LogicalType::RenderImage, Residency::Gpu,
                         RenderImageDescriptor())};
        schema.outputs = {Port("value", LogicalType::Scalar, Residency::Cpu)};
        schema.explicit_readback = true;
        schema.produces_resource = false;
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.render.fullscreen", OperatorKind::GpuPass,
            "Fullscreen shader pass producing a render image",
            kRuntimeFrequencies, Frequency::Frame, "nw.lower.hlsl.render");
        schema.inputs = {
            Port("shader", LogicalType::ShaderExpr, Residency::Logical),
            ResourcePort("texture", LogicalType::TextureRecipe,
                         Residency::Gpu, TextureDescriptor())};
        schema.outputs = {ResourcePort(
            "image", LogicalType::RenderImage, Residency::Gpu,
            RenderImageDescriptor())};
        schema.produces_resource = true;
        schema.resource_descriptor = RenderImageDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.render.gbuffer", OperatorKind::GpuPass,
            "G-buffer pass with typed multi-output render images",
            kRuntimeFrequencies, Frequency::Frame, "nw.lower.hlsl.render");
        schema.inputs = {
            ResourcePort("mesh", LogicalType::MeshRecipe, Residency::Gpu,
                         MeshDescriptor()),
            Port("material", LogicalType::Material, Residency::Cpu),
            Port("camera", LogicalType::Camera, Residency::Cpu),
            Port("light", LogicalType::Light, Residency::Cpu),
            Port("shader", LogicalType::ShaderExpr, Residency::Logical)};
        schema.outputs = {
            ResourcePort("color", LogicalType::RenderImage, Residency::Gpu,
                         RenderImageDescriptor()),
            ResourcePort("normals", LogicalType::RenderImage, Residency::Gpu,
                         RenderImageDescriptor())};
        schema.produces_resource = true;
        schema.resource_descriptor = RenderImageDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.render.composite", OperatorKind::GpuPass,
            "Composite typed render images", kRuntimeFrequencies,
            Frequency::Frame, "nw.lower.hlsl.render");
        schema.inputs = {
            ResourcePort("color", LogicalType::RenderImage, Residency::Gpu,
                         RenderImageDescriptor()),
            ResourcePort("normal", LogicalType::RenderImage, Residency::Gpu,
                         RenderImageDescriptor())};
        schema.outputs = {ResourcePort(
            "out", LogicalType::RenderImage, Residency::Gpu,
            RenderImageDescriptor())};
        schema.produces_resource = true;
        schema.resource_descriptor = RenderImageDescriptor();
        registry.Register(std::move(schema));
    }
    {
        OperatorSchema schema = BaseOperator(
            "nw.render.present", OperatorKind::GpuPass,
            "Presentable render image", kFrameFrequency, Frequency::Frame,
            "nw.lower.hlsl.render");
        schema.inputs = {
            ResourcePort("image", LogicalType::RenderImage, Residency::Gpu,
                         RenderImageDescriptor())};
        schema.outputs = {ResourcePort(
            "out", LogicalType::RenderImage, Residency::Gpu,
            RenderImageDescriptor())};
        schema.produces_resource = true;
        schema.resource_descriptor = RenderImageDescriptor();
        registry.Register(std::move(schema));
    }

    return registry;
}

// ---------------------------------------------------------------------------
// Canonical text helpers
// ---------------------------------------------------------------------------

namespace {

std::string EscapeQuoted(std::string_view text) {
    std::string out;
    out.push_back('"');
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
                std::snprintf(buffer, sizeof(buffer), "\\x%02x",
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

} // namespace

std::string CanonicalNumberText(double value) {
    if (!std::isfinite(value)) {
        return "nonfinite";
    }
    value = NormalizeZero(value);
    char buffer[64]{};
    std::snprintf(buffer, sizeof(buffer), "%a", value);
    return std::string(buffer);
}

std::string ParamCanonicalText(const ParamValue& value) {
    switch (value.type) {
    case ParamType::Float:
        return CanonicalNumberText(value.number);
    case ParamType::Int:
        return std::to_string(value.integer);
    case ParamType::Bool:
        return value.boolean ? "true" : "false";
    case ParamType::String:
    case ParamType::Enum:
    case ParamType::Version:
    case ParamType::Expression:
        return EscapeQuoted(value.text);
    case ParamType::Vec2:
    case ParamType::Vec3:
    case ParamType::Vec4:
    case ParamType::Color:
    case ParamType::Mat4: {
        std::string out;
        for (std::uint32_t i = 0; i < value.component_count; ++i) {
            if (i != 0) {
                out.push_back(' ');
            }
            out += CanonicalNumberText(value.components[i]);
        }
        return out;
    }
    }
    return {};
}

std::map<std::string, std::string> GoldenFieldDigests() {
    std::map<std::string, std::string> result;

    {
        CanonicalWriter writer;
        writer.ListHeader(6u);
        writer.String("nwgraph.golden.v1");
        writer.U64(1u);
        writer.Version(SemanticVersion{1, 2, 3});
        writer.String("hello");
        writer.Bool(true);
        writer.F64(0.25);
        result["base"] = "sha256:v1:" + RawDigestHex(writer.bytes());
    }
    {
        CanonicalWriter writer;
        writer.ListHeader(3u);
        writer.F64(0.1);
        writer.F64(-0.0);
        writer.F64(1e-1);
        result["float-normalization"] = "sha256:v1:" +
                                        RawDigestHex(writer.bytes());
    }
    {
        CanonicalWriter writer;
        writer.ListHeader(2u);
        writer.String("sha256:v1:" + std::string(64u, 'a'));
        writer.String("sha256:v1:" + std::string(64u, 'b'));
        result["upstream"] = "sha256:v1:" + RawDigestHex(writer.bytes());
    }
    {
        CanonicalWriter writer;
        writer.MapHeader(3u);
        writer.String("api");
        writer.String("d3d12");
        writer.String("deterministic");
        writer.String("true");
        writer.String("optimization");
        writer.String("O2");
        result["compiler-settings"] = "sha256:v1:" +
                                      RawDigestHex(writer.bytes());
    }
    {
        CanonicalWriter writer;
        writer.ListHeader(2u);
        EncodeQuantization(writer, QuantizationPolicy{
                                       "unorm8", 8u,
                                       0.0039215686274509803});
        writer.F64(0.5);
        result["quantization"] = "sha256:v1:" + RawDigestHex(writer.bytes());
    }
    {
        CanonicalWriter writer;
        writer.ListHeader(1u);
        writer.Version(SemanticVersion{1, 2, 3});
        result["version"] = "sha256:v1:" + RawDigestHex(writer.bytes());
    }
    return result;
}

// ---------------------------------------------------------------------------
// Diagnostics and capability profiles
// ---------------------------------------------------------------------------

std::string_view ToString(DiagnosticCode code) noexcept {
    switch (code) {
    case DiagnosticCode::UnknownSchemaVersion: return "unknown_schema_version";
    case DiagnosticCode::MigrationRequired: return "migration_required";
    case DiagnosticCode::UnsupportedMigration: return "unsupported_migration";
    case DiagnosticCode::UnknownOperator: return "unknown_operator";
    case DiagnosticCode::UnknownOperatorVersion:
        return "unknown_operator_version";
    case DiagnosticCode::DuplicateNodeId: return "duplicate_node_id";
    case DiagnosticCode::UnknownInputNode: return "unknown_input_node";
    case DiagnosticCode::UnknownInputPort: return "unknown_input_port";
    case DiagnosticCode::UnknownOutputPort: return "unknown_output_port";
    case DiagnosticCode::DuplicateInputPort: return "duplicate_input_port";
    case DiagnosticCode::MissingInputPort: return "missing_input_port";
    case DiagnosticCode::InvalidResidency: return "invalid_residency";
    case DiagnosticCode::PortTypeMismatch: return "port_type_mismatch";
    case DiagnosticCode::PortDimensionMismatch:
        return "port_dimension_mismatch";
    case DiagnosticCode::ImplicitReadback: return "implicit_readback";
    case DiagnosticCode::ImplicitUpload: return "implicit_upload";
    case DiagnosticCode::FrequencyTransition: return "frequency_transition";
    case DiagnosticCode::HistoryNotSupported: return "history_not_supported";
    case DiagnosticCode::UnsupportedFrequency: return "unsupported_frequency";
    case DiagnosticCode::OrdinaryCycle: return "ordinary_cycle";
    case DiagnosticCode::MissingParameter: return "missing_parameter";
    case DiagnosticCode::UnknownParameter: return "unknown_parameter";
    case DiagnosticCode::InvalidParameterType:
        return "invalid_parameter_type";
    case DiagnosticCode::NonFiniteParameter: return "non_finite_parameter";
    case DiagnosticCode::ParameterOutOfRange: return "parameter_out_of_range";
    case DiagnosticCode::BoundedResourceExceeded:
        return "bounded_resource_exceeded";
    case DiagnosticCode::InvalidParameterValue:
        return "invalid_parameter_value";
    case DiagnosticCode::InvalidSeedSource: return "invalid_seed_source";
    case DiagnosticCode::MissingExplicitSeed: return "missing_explicit_seed";
    case DiagnosticCode::UnsupportedSeedBehavior:
        return "unsupported_seed_behavior";
    case DiagnosticCode::MalformedCapabilityProfile:
        return "malformed_capability_profile";
    case DiagnosticCode::UnknownCapabilityProfile:
        return "unknown_capability_profile";
    case DiagnosticCode::CapabilityProfileDigestMismatch:
        return "capability_profile_digest_mismatch";
    case DiagnosticCode::InvalidCompilerSetting:
        return "invalid_compiler_setting";
    case DiagnosticCode::InvalidExpression: return "invalid_expression";
    case DiagnosticCode::EvaluationFailure: return "evaluation_failure";
    case DiagnosticCode::ParseError: return "parse_error";
    }
    return "parse_error";
}

bool HasErrors(const std::vector<Diagnostic>& diagnostics) noexcept {
    for (const auto& diagnostic : diagnostics) {
        if (diagnostic.severity == DiagnosticSeverity::Error) {
            return true;
        }
    }
    return false;
}

namespace {

std::string JsonEscape(std::string_view text) {
    std::string out;
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
    return out;
}

} // namespace

std::string DiagnosticToJson(const Diagnostic& diagnostic) {
    std::string out;
    out += "{";
    out += "\"severity\":\"" +
           std::string(diagnostic.severity == DiagnosticSeverity::Error
                           ? "error"
                           : diagnostic.severity == DiagnosticSeverity::Warning
                                 ? "warning"
                                 : "note") +
           "\"";
    out += ",\"code\":\"" + std::string(ToString(diagnostic.code)) + "\"";
    out += ",\"message\":\"" + JsonEscape(diagnostic.message) + "\"";
    out += ",\"node_id\":\"" + JsonEscape(diagnostic.source.node_id) + "\"";
    out += ",\"operator_id\":\"" +
           JsonEscape(diagnostic.source.operator_id) + "\"";
    out += ",\"port_id\":\"" + JsonEscape(diagnostic.source.port_id) + "\"";
    out += ",\"source_node_id\":\"" +
           JsonEscape(diagnostic.source.source_node_id) + "\"";
    out += ",\"source_port_id\":\"" +
           JsonEscape(diagnostic.source.source_port_id) + "\"";
    out += ",\"line\":" + std::to_string(diagnostic.source.line);
    out += ",\"column\":" + std::to_string(diagnostic.source.column);
    out += ",\"dependency_path\":[";
    for (std::size_t i = 0; i < diagnostic.dependency_path.size(); ++i) {
        if (i != 0) {
            out += ",";
        }
        out += "\"" + JsonEscape(diagnostic.dependency_path[i]) + "\"";
    }
    out += "]}";
    return out;
}

CapabilityProfileIdentity SelectTargetProfile(
    const ObservedHardwareMetadata&) noexcept {
    // Observed GPU model/generation/adapter/PCI metadata is deliberately not
    // consulted. The stable capability contract is the only authority.
    CapabilityProfileIdentity identity;
    identity.id = std::string(kCommonProfileId.data(), kCommonProfileId.size());
    identity.requirements_digest =
        std::string(kCommonRequirementsDigest.data(),
                    kCommonRequirementsDigest.size());
    return identity;
}

namespace {

Diagnostic MakeDiagnostic(DiagnosticCode code, std::string message,
                          DiagnosticSource source = {}) {
    Diagnostic diagnostic;
    diagnostic.severity = DiagnosticSeverity::Error;
    diagnostic.code = code;
    diagnostic.message = std::move(message);
    diagnostic.source = std::move(source);
    return diagnostic;
}

void ValidateCapabilityIdentity(const CapabilityProfileIdentity& identity,
                                std::vector<Diagnostic>& diagnostics) {
    DiagnosticSource source;
    if (!IsWellFormedProfileId(identity.id)) {
        diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::MalformedCapabilityProfile,
            "malformed capability-profile id '" + identity.id + "'", source));
        return;
    }
    if (!IsWellFormedDigest(identity.requirements_digest)) {
        diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::MalformedCapabilityProfile,
            "malformed capability-profile requirements digest '" +
                identity.requirements_digest + "'",
            source));
        return;
    }
    if (identity.id !=
        std::string(kCommonProfileId.data(), kCommonProfileId.size())) {
        diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::UnknownCapabilityProfile,
            "unknown capability-profile identity '" + identity.id + "'",
            source));
        return;
    }
    if (identity.requirements_digest !=
        std::string(kCommonRequirementsDigest.data(),
                    kCommonRequirementsDigest.size())) {
        diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::CapabilityProfileDigestMismatch,
            "capability-profile '" + identity.id +
                "' requires requirements digest '" +
                std::string(kCommonRequirementsDigest.data(),
                            kCommonRequirementsDigest.size()) +
                "', got '" +
                identity.requirements_digest + "'",
            source));
    }
}

bool ValidateCompilerSettings(const CompilerSettings& settings,
                              std::vector<Diagnostic>& diagnostics) {
    bool ok = true;
    auto error = [&](const std::string& message) {
        diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::InvalidCompilerSetting, message));
        ok = false;
    };
    if (settings.optimization != "O0" && settings.optimization != "O1" &&
        settings.optimization != "O2" && settings.optimization != "O3") {
        error("unsupported compiler optimization '" + settings.optimization +
              "'");
    }
    if (settings.api != "d3d12") {
        error("unsupported compiler api '" + settings.api + "'");
    }
    if (settings.feature_level != "12_0") {
        error("unsupported compiler feature_level '" + settings.feature_level +
              "'");
    }
    if (settings.shader_model != "6_0") {
        error("unsupported compiler shader_model '" + settings.shader_model +
              "'");
    }
    if (settings.frequency_policy != "strict") {
        error("unsupported frequency_policy '" + settings.frequency_policy +
              "'");
    }
    if (settings.shader_blob_policy != "stable") {
        error("unsupported shader_blob_policy '" +
              settings.shader_blob_policy + "'");
    }
    static const std::set<std::string> kQuantizationPolicies = {
        "none", "unorm8", "uint8", "float16"};
    if (kQuantizationPolicies.count(settings.quantization.policy) == 0u) {
        error("unsupported quantization policy '" +
              settings.quantization.policy + "'");
    }
    if (settings.quantization.bits > 64u) {
        error("quantization bits must not exceed 64");
    }
    if (!std::isfinite(settings.quantization.step) ||
        settings.quantization.step < 0.0) {
        error("quantization step must be finite and non-negative");
    }
    if (settings.quantization.policy == "none" &&
        (settings.quantization.bits != 0u ||
         settings.quantization.step != 0.0)) {
        error("quantization policy 'none' requires bits=0 and step=0");
    }
    return ok;
}

} // namespace





namespace {

Diagnostic MakeDiagnostic(DiagnosticCode code, std::string message,
                          DiagnosticSource source);

struct TokenizeResult {
    std::vector<std::string> tokens;
    bool ok = true;
    std::string error;
};

TokenizeResult TokenizeLine(std::string_view line) {
    TokenizeResult result;
    std::string token;
    bool in_quotes = false;
    bool token_started = false;
    for (std::size_t i = 0; i < line.size(); ++i) {
        const char c = line[i];
        if (in_quotes) {
            if (c == '\\' && i + 1u < line.size()) {
                const char escaped = line[++i];
                switch (escaped) {
                case 'n': token.push_back('\n'); break;
                case 'r': token.push_back('\r'); break;
                case 't': token.push_back('\t'); break;
                case '\\': token.push_back('\\'); break;
                case '"': token.push_back('"'); break;
                case 'x': {
                    if (i + 2u < line.size() &&
                        IsHexDigit(line[i + 1u]) &&
                        IsHexDigit(line[i + 2u])) {
                        auto hex_value = [](char c) -> int {
                            if (c >= '0' && c <= '9') return c - '0';
                            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
                            return c - 'A' + 10;
                        };
                        const int high = hex_value(line[i + 1u]);
                        const int low = hex_value(line[i + 2u]);
                        token.push_back(
                            static_cast<char>((high << 4) | low));
                        i += 2u;
                    } else {
                        token.push_back('x');
                    }
                    break;
                }
                default: token.push_back(escaped); break;
                }
            } else if (c == '"') {
                in_quotes = false;
                token_started = true;
            } else {
                token.push_back(c);
            }
            continue;
        }
        if (c == '#') {
            break;
        }
        if (c == '"') {
            in_quotes = true;
            token_started = true;
            continue;
        }
        if (std::isspace(static_cast<unsigned char>(c))) {
            if (token_started || !token.empty()) {
                result.tokens.push_back(token);
                token.clear();
                token_started = false;
            }
            continue;
        }
        token.push_back(c);
        token_started = true;
    }
    if (in_quotes) {
        result.ok = false;
        result.error = "unterminated quoted string";
        return result;
    }
    if (token_started || !token.empty()) {
        result.tokens.push_back(token);
    }
    return result;
}

std::vector<std::string> SplitCommaTokens(const std::vector<std::string>& tokens) {
    std::vector<std::string> out;
    for (const auto& token : tokens) {
        std::size_t start = 0;
        for (std::size_t i = 0; i <= token.size(); ++i) {
            if (i == token.size() || token[i] == ',') {
                if (i > start) {
                    out.push_back(token.substr(start, i - start));
                }
                start = i + 1u;
            }
        }
    }
    return out;
}

bool IsValidExpression(std::string_view expression, std::string& reason) {
    if (expression.empty()) {
        reason = "shader expression is empty";
        return false;
    }
    static constexpr std::string_view kForbidden[] = {
        ";", "{", "}", "#", "return", "void main", "asm", "uniform",
        "register", "struct", "class", "include", "compile"};
    for (std::string_view forbidden : kForbidden) {
        if (expression.find(forbidden) != std::string_view::npos) {
            reason = "shader expression contains unsupported construct '" +
                     std::string(forbidden) + "'";
            return false;
        }
    }
    for (char c : expression) {
        if (c == '`' || c == '$' || c == '@') {
            reason = "shader expression contains unsupported character";
            return false;
        }
    }
    return true;
}

bool IsEnumValue(const ParamSpec& spec, std::string_view value) {
    for (const auto& candidate : spec.enum_values) {
        if (std::string_view(candidate) == value) {
            return true;
        }
    }
    return false;
}

bool CheckFiniteAndRange(const ParamSpec& spec, const ParamValue& value,
                         const DiagnosticSource& source,
                         std::vector<Diagnostic>& diagnostics) {
    if (AnyComponentNonFinite(value)) {
        diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::NonFiniteParameter,
            "parameter '" + spec.id + "' must be finite", source));
        return false;
    }
    if (!spec.has_range) {
        return true;
    }
    auto check_one = [&](double number) {
        if (number < spec.min_value || number > spec.max_value) {
            diagnostics.push_back(MakeDiagnostic(
                spec.bounded_resource_dimension
                    ? DiagnosticCode::BoundedResourceExceeded
                    : DiagnosticCode::ParameterOutOfRange,
                "parameter '" + spec.id + "' value " +
                    CanonicalNumberText(number) + " lies outside [" +
                    CanonicalNumberText(spec.min_value) + ", " +
                    CanonicalNumberText(spec.max_value) + "]",
                source));
            return false;
        }
        return true;
    };
    if (value.type == ParamType::Float) {
        return check_one(value.number);
    }
    if (value.type == ParamType::Int) {
        return check_one(static_cast<double>(value.integer));
    }
    if (value.type == ParamType::Vec2 || value.type == ParamType::Vec3 ||
        value.type == ParamType::Vec4 || value.type == ParamType::Color ||
        value.type == ParamType::Mat4) {
        for (std::uint32_t i = 0; i < value.component_count; ++i) {
            if (!check_one(value.components[i])) {
                return false;
            }
        }
    }
    return true;
}

std::optional<ParamValue> ParseParamTokens(
    const ParamSpec& spec, const std::vector<std::string>& tokens,
    const DiagnosticSource& source, std::vector<Diagnostic>& diagnostics) {
    ParamValue value;
    value.type = spec.type;
    value.authoring_text = Join(tokens, " ");

    auto failure = [&](DiagnosticCode code, const std::string& message) {
        diagnostics.push_back(MakeDiagnostic(code, message, source));
        return std::nullopt;
    };

    if (spec.type == ParamType::Float) {
        if (tokens.size() != 1u) {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id +
                               "' expects exactly one floating-point value");
        }
        double parsed = 0.0;
        if (!ParseDouble(tokens[0], parsed)) {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id +
                               "' is not a valid floating-point value");
        }
        value.number = NormalizeZero(parsed);
        if (!CheckFiniteAndRange(spec, value, source, diagnostics)) {
            return std::nullopt;
        }
        return value;
    }
    if (spec.type == ParamType::Int) {
        if (tokens.size() != 1u) {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id +
                               "' expects exactly one integer value");
        }
        std::int64_t parsed = 0;
        if (!FromCharsI64(tokens[0], parsed)) {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id +
                               "' is not a valid integer value");
        }
        value.integer = parsed;
        if (!CheckFiniteAndRange(spec, value, source, diagnostics)) {
            return std::nullopt;
        }
        return value;
    }
    if (spec.type == ParamType::Bool) {
        if (tokens.size() != 1u) {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id +
                               "' expects exactly one boolean value");
        }
        const std::string lowered = Lower(tokens[0]);
        if (lowered == "true" || lowered == "1") {
            value.boolean = true;
        } else if (lowered == "false" || lowered == "0") {
            value.boolean = false;
        } else {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id +
                               "' expects true/false");
        }
        return value;
    }
    if (spec.type == ParamType::String || spec.type == ParamType::Enum ||
        spec.type == ParamType::Version ||
        spec.type == ParamType::Expression) {
        value.text = Join(tokens, " ");
        if (spec.type == ParamType::Enum &&
            !IsEnumValue(spec, value.text)) {
            return failure(DiagnosticCode::InvalidParameterValue,
                           "parameter '" + spec.id + "' value '" + value.text +
                               "' is not one of the declared enum values");
        }
        if (spec.type == ParamType::Expression) {
            std::string reason;
            if (!IsValidExpression(value.text, reason)) {
                return failure(DiagnosticCode::InvalidExpression, reason);
            }
        }
        return value;
    }

    // Vector / matrix component values.
    std::vector<std::string> parts =
        tokens.size() == 1u ? SplitCommaTokens(tokens) : tokens;
    if (spec.type == ParamType::Mat4) {
        if (parts.size() == 1u && Lower(parts[0]) == "identity") {
            value.component_count = 16u;
            for (std::uint32_t i = 0; i < 4u; ++i) {
                value.components[i * 5u] = 1.0;
            }
            return value;
        }
        if (parts.size() != 16u) {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id +
                               "' expects 16 matrix components or 'identity'");
        }
    } else {
        std::uint32_t expected = 0;
        if (spec.type == ParamType::Vec2) expected = 2u;
        if (spec.type == ParamType::Vec3) expected = 3u;
        if (spec.type == ParamType::Vec4 || spec.type == ParamType::Color) {
            expected = 4u;
        }
        if (spec.type == ParamType::Color && parts.size() == 3u) {
            parts.push_back("1");
        }
        if (parts.size() != expected) {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id + "' expects " +
                               std::to_string(expected) + " components");
        }
    }
    value.component_count = static_cast<std::uint32_t>(parts.size());
    for (std::size_t i = 0; i < parts.size(); ++i) {
        double parsed = 0.0;
        if (!ParseDouble(parts[i], parsed)) {
            return failure(DiagnosticCode::InvalidParameterType,
                           "parameter '" + spec.id +
                               "' contains a non-numeric component");
        }
        value.components[i] = NormalizeZero(parsed);
    }
    if (spec.normalization == NormalizationRule::NormalizeVector &&
        value.component_count != 0u) {
        double length_squared = 0.0;
        for (std::uint32_t i = 0; i < value.component_count; ++i) {
            length_squared += value.components[i] * value.components[i];
        }
        if (!(length_squared > 0.0) || !std::isfinite(length_squared)) {
            return failure(DiagnosticCode::InvalidParameterValue,
                           "parameter '" + spec.id +
                               "' cannot be normalized (zero or non-finite)");
        }
        const double inverse = 1.0 / std::sqrt(length_squared);
        for (std::uint32_t i = 0; i < value.component_count; ++i) {
            value.components[i] = NormalizeZero(value.components[i] * inverse);
        }
    }
    if (!CheckFiniteAndRange(spec, value, source, diagnostics)) {
        return std::nullopt;
    }
    return value;
}

bool ApplySetting(const std::string& key, const std::string& value,
                  CompilerSettings& settings, std::vector<Diagnostic>& diagnostics,
                  int line) {
    DiagnosticSource source;
    source.line = line;
    auto invalid = [&](const std::string& message) {
        diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::InvalidCompilerSetting, message, source));
        return false;
    };
    if (key == "optimization") {
        settings.optimization = value;
    } else if (key == "deterministic") {
        const std::string lowered = Lower(value);
        if (lowered == "true" || lowered == "1") {
            settings.deterministic = true;
        } else if (lowered == "false" || lowered == "0") {
            settings.deterministic = false;
        } else {
            return invalid("deterministic must be true or false");
        }
    } else if (key == "api") {
        settings.api = value;
    } else if (key == "feature_level") {
        settings.feature_level = value;
    } else if (key == "shader_model") {
        settings.shader_model = value;
    } else if (key == "frequency_policy") {
        settings.frequency_policy = value;
    } else if (key == "shader_blob_policy") {
        settings.shader_blob_policy = value;
    } else if (key == "quantization_policy") {
        settings.quantization.policy = value;
    } else if (key == "quantization_bits") {
        std::uint32_t bits = 0;
        if (!FromCharsU32(value, bits)) {
            return invalid("quantization_bits must be an unsigned integer");
        }
        settings.quantization.bits = bits;
    } else if (key == "quantization_step") {
        double step = 0.0;
        if (!ParseDouble(value, step)) {
            return invalid("quantization_step must be a number");
        }
        settings.quantization.step = NormalizeZero(step);
    } else {
        return invalid("unknown compiler setting '" + key + "'");
    }
    return true;
}

} // namespace

ParseResult ParseGraphText(std::string_view text,
                           const OperatorRegistry& registry,
                           std::string source_name) {
    ParseResult result;
    GraphDocument document;
    document.source_name = std::move(source_name);
    bool saw_header = false;
    std::size_t current_node = std::string::npos;
    std::set<std::string> node_ids;

    std::size_t line_start = 0;
    int line_number = 0;
    while (line_start <= text.size()) {
        const std::size_t line_end = text.find('\n', line_start);
        const std::size_t end =
            line_end == std::string_view::npos ? text.size() : line_end;
        const std::string_view line = text.substr(line_start, end - line_start);
        ++line_number;
        line_start = end + 1u;

        TokenizeResult tokenized = TokenizeLine(line);
        if (!tokenized.ok) {
            DiagnosticSource source;
            source.line = line_number;
            source.column = 1;
            result.diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::ParseError, tokenized.error, source));
            if (line_end == std::string_view::npos) {
                break;
            }
            continue;
        }
        if (tokenized.tokens.empty()) {
            if (line_end == std::string_view::npos) {
                break;
            }
            continue;
        }
        const std::vector<std::string>& tokens = tokenized.tokens;
        const std::string& directive = tokens[0];

        if (directive == "nwgraph") {
            current_node = std::string::npos;
            std::uint32_t version = 0;
            if (tokens.size() != 2u || !FromCharsU32(tokens[1], version)) {
                DiagnosticSource source;
                source.line = line_number;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "expected 'nwgraph <version>'", source));
            } else {
                document.schema_version = version;
                saw_header = true;
            }
        } else if (directive == "profile") {
            current_node = std::string::npos;
            if (tokens.size() != 3u) {
                DiagnosticSource source;
                source.line = line_number;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "expected 'profile <id> <requirements-digest>'", source));
            } else {
                document.profile.id = tokens[1];
                document.profile.requirements_digest = tokens[2];
            }
        } else if (directive == "setting") {
            current_node = std::string::npos;
            if (tokens.size() != 3u) {
                DiagnosticSource source;
                source.line = line_number;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "expected 'setting <key> <value>'", source));
            } else {
                ApplySetting(tokens[1], tokens[2], document.settings,
                             result.diagnostics, line_number);
            }
        } else if (directive == "node") {
            if (tokens.size() < 3u) {
                DiagnosticSource source;
                source.line = line_number;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "expected 'node <id> <operator>@<version>'", source));
                current_node = std::string::npos;
                if (line_end == std::string_view::npos) break;
                continue;
            }
            NodeDefinition node;
            node.id = tokens[1];
            const std::string operator_token = tokens[2];
            std::string operator_id = operator_token;
            SemanticVersion operator_version{};
            bool has_version = false;
            const std::size_t at = operator_token.rfind('@');
            if (at != std::string::npos) {
                operator_id = operator_token.substr(0, at);
                const auto parsed_version =
                    SemanticVersion::Parse(operator_token.substr(at + 1u));
                if (!parsed_version) {
                    DiagnosticSource source;
                    source.line = line_number;
                    source.node_id = node.id;
                    result.diagnostics.push_back(MakeDiagnostic(
                        DiagnosticCode::ParseError,
                        "malformed operator version in '" + operator_token + "'",
                        source));
                } else {
                    operator_version = *parsed_version;
                    has_version = true;
                }
            }
            node.operator_id = operator_id;
            node.operator_version = operator_version;

            const OperatorSchema* schema = nullptr;
            if (has_version) {
                schema = registry.Find(operator_id, operator_version);
                if (schema == nullptr) {
                    DiagnosticSource source;
                    source.line = line_number;
                    source.node_id = node.id;
                    source.operator_id = operator_id;
                    if (registry.FindAny(operator_id) != nullptr) {
                        result.diagnostics.push_back(MakeDiagnostic(
                            DiagnosticCode::UnknownOperatorVersion,
                            "unknown operator version '" + operator_id + "@" +
                                operator_version.ToString() + "'",
                            source));
                    } else {
                        result.diagnostics.push_back(MakeDiagnostic(
                            DiagnosticCode::UnknownOperator,
                            "unknown operator '" + operator_id + "'", source));
                    }
                }
            } else {
                schema = registry.FindAny(operator_id);
                if (schema == nullptr) {
                    DiagnosticSource source;
                    source.line = line_number;
                    source.node_id = node.id;
                    source.operator_id = operator_id;
                    result.diagnostics.push_back(MakeDiagnostic(
                        DiagnosticCode::UnknownOperator,
                        "unknown operator '" + operator_id + "'", source));
                } else if (document.schema_version != 0u) {
                    DiagnosticSource source;
                    source.line = line_number;
                    source.node_id = node.id;
                    source.operator_id = operator_id;
                    result.diagnostics.push_back(MakeDiagnostic(
                        DiagnosticCode::ParseError,
                        "schema v1 nodes must declare an explicit operator "
                        "version ('" +
                            operator_id + "@x.y.z')",
                        source));
                }
            }
            if (schema != nullptr) {
                node.frequency = schema->frequency_policy.default_frequency;
                node.residency = schema->PrimaryResidency();
                node.seed.behavior = schema->default_seed_behavior;
                for (const auto& param : schema->params) {
                    if (param.default_value) {
                        node.params[param.id] = *param.default_value;
                    }
                }
            } else {
                node.frequency = Frequency::Frame;
                node.residency = Residency::Cpu;
            }

            for (std::size_t i = 3u; i < tokens.size(); ++i) {
                const std::string& attribute = tokens[i];
                const std::size_t equals = attribute.find('=');
                if (equals != std::string::npos) {
                    const std::string key = attribute.substr(0, equals);
                    const std::string value = attribute.substr(equals + 1u);
                    if (key == "frequency") {
                        const auto frequency = ParseFrequency(value);
                        if (!frequency) {
                            DiagnosticSource source;
                            source.line = line_number;
                            source.node_id = node.id;
                            result.diagnostics.push_back(MakeDiagnostic(
                                DiagnosticCode::UnsupportedFrequency,
                                "unsupported frequency '" + value + "'", source));
                        } else {
                            node.frequency = *frequency;
                        }
                    } else if (key == "residency") {
                        const auto residency = ParseResidency(value);
                        if (!residency) {
                            DiagnosticSource source;
                            source.line = line_number;
                            source.node_id = node.id;
                            result.diagnostics.push_back(MakeDiagnostic(
                                DiagnosticCode::InvalidResidency,
                                "unsupported residency '" + value + "'", source));
                        } else {
                            node.residency = *residency;
                            node.has_declared_residency = true;
                        }
                    } else if (key == "seed") {
                        std::uint64_t seed = 0;
                        if (FromCharsU64(value, seed)) {
                            node.seed.behavior = SeedBehavior::Explicit;
                            node.seed.has_explicit_seed = true;
                            node.seed.explicit_seed = seed;
                        } else if (const auto behavior =
                                       ParseSeedBehavior(value)) {
                            node.seed.behavior = *behavior;
                        } else {
                            DiagnosticSource source;
                            source.line = line_number;
                            source.node_id = node.id;
                            result.diagnostics.push_back(MakeDiagnostic(
                                DiagnosticCode::ParseError,
                                "unsupported seed attribute '" + value + "'",
                                source));
                        }
                    } else {
                        DiagnosticSource source;
                        source.line = line_number;
                        source.node_id = node.id;
                        result.diagnostics.push_back(MakeDiagnostic(
                            DiagnosticCode::ParseError,
                            "unknown node attribute '" + key + "'", source));
                    }
                } else if (document.schema_version == 0u) {
                    const auto frequency = ParseFrequency(attribute);
                    if (frequency) {
                        node.frequency = *frequency;
                    } else {
                        DiagnosticSource source;
                        source.line = line_number;
                        source.node_id = node.id;
                        result.diagnostics.push_back(MakeDiagnostic(
                            DiagnosticCode::ParseError,
                            "unknown legacy node attribute '" + attribute + "'",
                            source));
                    }
                } else {
                    DiagnosticSource source;
                    source.line = line_number;
                    source.node_id = node.id;
                    result.diagnostics.push_back(MakeDiagnostic(
                        DiagnosticCode::ParseError,
                        "unknown node attribute '" + attribute + "'", source));
                }
            }
            if (!node_ids.insert(node.id).second) {
                DiagnosticSource source;
                source.line = line_number;
                source.node_id = node.id;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::DuplicateNodeId,
                    "duplicate node id '" + node.id + "'", source));
            }
            document.nodes.push_back(std::move(node));
            current_node = document.nodes.size() - 1u;
        } else if (directive == "param") {
            if (current_node == std::string::npos) {
                DiagnosticSource source;
                source.line = line_number;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "'param' must follow a node declaration", source));
                if (line_end == std::string_view::npos) break;
                continue;
            }
            NodeDefinition& node = document.nodes[current_node];
            if (tokens.size() < 3u) {
                DiagnosticSource source;
                source.line = line_number;
                source.node_id = node.id;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "expected 'param <name> <value...>'", source));
                if (line_end == std::string_view::npos) break;
                continue;
            }
            const OperatorSchema* schema =
                registry.Find(node.operator_id, node.operator_version);
            if (schema == nullptr) {
                schema = registry.FindAny(node.operator_id);
            }
            if (schema == nullptr) {
                if (line_end == std::string_view::npos) break;
                continue;
            }
            const ParamSpec* spec = schema->FindParam(tokens[1]);
            DiagnosticSource source;
            source.line = line_number;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            if (spec == nullptr) {
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::UnknownParameter,
                    "unknown parameter '" + tokens[1] + "' for operator '" +
                        node.operator_id + "'",
                    source));
                if (line_end == std::string_view::npos) break;
                continue;
            }
            std::vector<std::string> values(tokens.begin() + 2, tokens.end());
            auto parsed = ParseParamTokens(*spec, values, source,
                                           result.diagnostics);
            if (parsed) {
                node.params[spec->id] = std::move(*parsed);
            }
        } else if (directive == "input") {
            if (current_node == std::string::npos) {
                DiagnosticSource source;
                source.line = line_number;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "'input' must follow a node declaration", source));
                if (line_end == std::string_view::npos) break;
                continue;
            }
            if (tokens.size() != 3u) {
                DiagnosticSource source;
                source.line = line_number;
                source.node_id = document.nodes[current_node].id;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "expected 'input <port> <source-node>.<source-port>'",
                    source));
                if (line_end == std::string_view::npos) break;
                continue;
            }
            const std::size_t dot = tokens[2].rfind('.');
            if (dot == std::string::npos || dot == 0u ||
                dot + 1u >= tokens[2].size()) {
                DiagnosticSource source;
                source.line = line_number;
                source.node_id = document.nodes[current_node].id;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "input source must be '<source-node>.<source-port>'",
                    source));
                if (line_end == std::string_view::npos) break;
                continue;
            }
            NodeInput input;
            input.port = tokens[1];
            input.source_node = tokens[2].substr(0, dot);
            input.source_port = tokens[2].substr(dot + 1u);
            document.nodes[current_node].inputs.push_back(std::move(input));
        } else if (directive == "seed") {
            if (current_node == std::string::npos) {
                DiagnosticSource source;
                source.line = line_number;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "'seed' must follow a node declaration", source));
                if (line_end == std::string_view::npos) break;
                continue;
            }
            NodeDefinition& node = document.nodes[current_node];
            DiagnosticSource source;
            source.line = line_number;
            source.node_id = node.id;
            bool parsed = false;
            if (tokens.size() == 2u) {
                std::uint64_t seed = 0;
                if (FromCharsU64(tokens[1], seed)) {
                    node.seed.behavior = SeedBehavior::Explicit;
                    node.seed.has_explicit_seed = true;
                    node.seed.explicit_seed = seed;
                    parsed = true;
                } else if (const auto behavior = ParseSeedBehavior(tokens[1])) {
                    node.seed.behavior = *behavior;
                    parsed = true;
                }
            } else if (tokens.size() == 3u &&
                       tokens[1] == "node-id") {
                node.seed.behavior = SeedBehavior::ExplicitNodeId;
                node.seed.node_id = tokens[2];
                parsed = true;
            } else if (tokens.size() == 3u &&
                       tokens[1] == "explicit") {
                std::uint64_t seed = 0;
                if (FromCharsU64(tokens[2], seed)) {
                    node.seed.behavior = SeedBehavior::Explicit;
                    node.seed.has_explicit_seed = true;
                    node.seed.explicit_seed = seed;
                    parsed = true;
                }
            }
            if (!parsed) {
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "expected 'seed <value>', 'seed <behavior>', "
                    "'seed explicit <value>' or 'seed node-id <node>'",
                    source));
            }
        } else if (directive == "layout") {
            current_node = std::string::npos;
            if (tokens.size() < 4u) {
                DiagnosticSource source;
                source.line = line_number;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::ParseError,
                    "expected 'layout <node> <x> <y> [collapsed=true]'",
                    source));
            } else {
                LayoutNode layout;
                double x = 0.0;
                double y = 0.0;
                if (!ParseDouble(tokens[2], x) || !ParseDouble(tokens[3], y) ||
                    !std::isfinite(x) || !std::isfinite(y)) {
                    DiagnosticSource source;
                    source.line = line_number;
                    result.diagnostics.push_back(MakeDiagnostic(
                        DiagnosticCode::ParseError,
                        "layout coordinates must be finite numbers", source));
                } else {
                    layout.x = NormalizeZero(x);
                    layout.y = NormalizeZero(y);
                    if (tokens.size() >= 5u) {
                        const std::size_t equals = tokens[4].find('=');
                        if (equals != std::string::npos &&
                            tokens[4].substr(0, equals) == "collapsed") {
                            layout.collapsed =
                                Lower(tokens[4].substr(equals + 1u)) == "true";
                        }
                    }
                    document.layout.nodes[tokens[1]] = layout;
                }
            }
        } else {
            DiagnosticSource source;
            source.line = line_number;
            result.diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::ParseError,
                "unknown directive '" + directive + "'", source));
        }

        if (line_end == std::string_view::npos) {
            break;
        }
    }

    if (!saw_header) {
        result.diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::ParseError,
            "missing 'nwgraph <version>' header"));
    }
    result.document = std::move(document);
    return result;
}


// ---------------------------------------------------------------------------
// Validation, topology, and canonical snapshot construction
// ---------------------------------------------------------------------------

namespace {

struct TopologyResult {
    bool ok = false;
    std::vector<std::string> order;
    std::vector<Diagnostic> diagnostics;
};

TopologyResult ComputeTopology(const GraphDocument& document) {
    TopologyResult result;
    std::map<std::string, std::size_t> index;
    for (std::size_t i = 0; i < document.nodes.size(); ++i) {
        index[document.nodes[i].id] = i;
    }
    std::map<std::string, int> indegree;
    std::map<std::string, std::vector<std::string>> adjacency;
    for (const auto& node : document.nodes) {
        indegree[node.id] = 0;
    }
    for (const auto& node : document.nodes) {
        for (const auto& input : node.inputs) {
            if (index.count(input.source_node) == 0u) {
                continue;
            }
            adjacency[input.source_node].push_back(node.id);
            ++indegree[node.id];
        }
    }
    std::set<std::string> ready;
    for (const auto& [id, degree] : indegree) {
        if (degree == 0) {
            ready.insert(id);
        }
    }
    while (!ready.empty()) {
        const std::string id = *ready.begin();
        ready.erase(ready.begin());
        result.order.push_back(id);
        for (const auto& consumer : adjacency[id]) {
            --indegree[consumer];
            if (indegree[consumer] == 0) {
                ready.insert(consumer);
            }
        }
    }
    if (result.order.size() == document.nodes.size()) {
        result.ok = true;
        return result;
    }
    std::set<std::string> remaining;
    for (const auto& [id, degree] : indegree) {
        if (degree > 0) {
            remaining.insert(id);
        }
    }
    for (const auto& node : document.nodes) {
        if (remaining.count(node.id) == 0u) {
            continue;
        }
        for (const auto& input : node.inputs) {
            if (remaining.count(input.source_node) == 0u) {
                continue;
            }
            DiagnosticSource source;
            source.node_id = node.id;
            source.port_id = input.port;
            source.source_node_id = input.source_node;
            source.source_port_id = input.source_port;
            Diagnostic diagnostic = MakeDiagnostic(
                DiagnosticCode::OrdinaryCycle,
                "ordinary cycle edge " + input.source_node + "." +
                    input.source_port + " -> " + node.id + "." + input.port,
                source);
            diagnostic.dependency_path = {
                input.source_node + "." + input.source_port + " -> " +
                node.id + "." + input.port};
            result.diagnostics.push_back(std::move(diagnostic));
        }
    }
    return result;
}

void ValidateParams(const NodeDefinition& node, const OperatorSchema& schema,
                    std::vector<Diagnostic>& diagnostics) {
    for (const auto& spec : schema.params) {
        const auto found = node.params.find(spec.id);
        if (found == node.params.end()) {
            if (spec.required) {
                DiagnosticSource source;
                source.node_id = node.id;
                source.operator_id = node.operator_id;
                source.port_id = spec.id;
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::MissingParameter,
                    "node '" + node.id + "' is missing required parameter '" +
                        spec.id + "'",
                    source));
            }
            continue;
        }
        const ParamValue& value = found->second;
        DiagnosticSource source;
        source.node_id = node.id;
        source.operator_id = node.operator_id;
        source.port_id = spec.id;
        if (value.type != spec.type) {
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::InvalidParameterType,
                "parameter '" + spec.id + "' has type '" +
                    std::string(ToString(value.type)) + "', expected '" +
                    std::string(ToString(spec.type)) + "'",
                source));
            continue;
        }
        if (AnyComponentNonFinite(value)) {
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::NonFiniteParameter,
                "parameter '" + spec.id +
                    "' must contain only finite semantic values",
                source));
            continue;
        }
        if (spec.type == ParamType::Enum && !IsEnumValue(spec, value.text)) {
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::InvalidParameterValue,
                "parameter '" + spec.id + "' value '" + value.text +
                    "' is not one of the declared enum values",
                source));
        }
        if (spec.type == ParamType::Expression) {
            std::string reason;
            if (!IsValidExpression(value.text, reason)) {
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::InvalidExpression, reason, source));
            }
        }
        if (spec.normalization == NormalizationRule::NormalizeVector &&
            value.component_count != 0u) {
            double length_squared = 0.0;
            for (std::uint32_t i = 0; i < value.component_count; ++i) {
                length_squared += value.components[i] * value.components[i];
            }
            if (!(length_squared > 0.0) || !std::isfinite(length_squared)) {
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::InvalidParameterValue,
                    "parameter '" + spec.id +
                        "' cannot be normalized (zero or non-finite)",
                    source));
            }
        }
        CheckFiniteAndRange(spec, value, source, diagnostics);
    }
    for (const auto& [name, value] : node.params) {
        (void)value;
        if (schema.FindParam(name) == nullptr) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            source.port_id = name;
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::UnknownParameter,
                "unknown parameter '" + name + "' for operator '" +
                    schema.id + "'",
                source));
        }
    }
}

void FillDefaultParams(GraphDocument& document,
                       const OperatorRegistry& registry) {
    for (auto& node : document.nodes) {
        const OperatorSchema* schema =
            registry.Find(node.operator_id, node.operator_version);
        if (schema == nullptr) {
            schema = registry.FindAny(node.operator_id);
        }
        if (schema == nullptr) {
            continue;
        }
        if (!node.has_declared_residency) {
            node.residency = schema->PrimaryResidency();
        }
        for (const auto& param : schema->params) {
            if (param.default_value &&
                node.params.count(param.id) == 0u) {
                node.params[param.id] = *param.default_value;
            }
        }
    }
}

std::string NodeSeedText(const NodeDefinition& node) {
    std::string text = std::string(ToString(node.seed.behavior));
    if (node.seed.behavior == SeedBehavior::Explicit) {
        text += ":" + std::to_string(node.seed.explicit_seed);
    } else if (node.seed.behavior == SeedBehavior::ExplicitNodeId) {
        text += ":" + node.seed.node_id;
    }
    return text;
}

std::vector<std::uint8_t> BuildCanonicalSemanticBytes(
    const GraphDocument& document,
    const std::map<std::string, std::string>& structural_hashes) {
    CanonicalWriter writer;
    writer.String("nwgraph.semantic.v1");
    writer.U64(document.schema_version);
    writer.String(document.profile.id);
    writer.String(document.profile.requirements_digest);
    EncodeCompilerSettings(writer, document.settings);
    writer.String("nw.compiler.graph.v1");
    std::vector<std::string> hashes;
    hashes.reserve(structural_hashes.size());
    for (const auto& [node_id, hash] : structural_hashes) {
        (void)node_id;
        hashes.push_back(hash);
    }
    std::sort(hashes.begin(), hashes.end());
    writer.ListHeader(static_cast<std::uint32_t>(hashes.size()));
    for (const auto& hash : hashes) {
        writer.String(hash);
    }
    return writer.Take();
}

std::string BuildCanonicalText(
    const GraphDocument& document,
    const std::map<std::string, std::string>& structural_hashes) {
    std::ostringstream out;
    out << "nwgraph " << document.schema_version << "\n";
    out << "profile " << document.profile.id << " "
        << document.profile.requirements_digest << "\n";
    out << "setting optimization " << document.settings.optimization << "\n";
    out << "setting deterministic "
        << (document.settings.deterministic ? "true" : "false") << "\n";
    out << "setting api " << document.settings.api << "\n";
    out << "setting feature_level " << document.settings.feature_level << "\n";
    out << "setting shader_model " << document.settings.shader_model << "\n";
    out << "setting frequency_policy " << document.settings.frequency_policy
        << "\n";
    out << "setting shader_blob_policy "
        << document.settings.shader_blob_policy << "\n";
    out << "setting quantization_policy "
        << document.settings.quantization.policy << "\n";
    out << "setting quantization_bits " << document.settings.quantization.bits
        << "\n";
    out << "setting quantization_step "
        << CanonicalNumberText(document.settings.quantization.step) << "\n";

    std::vector<const NodeDefinition*> sorted;
    sorted.reserve(document.nodes.size());
    for (const auto& node : document.nodes) {
        sorted.push_back(&node);
    }
    std::sort(sorted.begin(), sorted.end(),
              [&](const NodeDefinition* a, const NodeDefinition* b) {
                  const std::string& ha = structural_hashes.at(a->id);
                  const std::string& hb = structural_hashes.at(b->id);
                  if (ha != hb) return ha < hb;
                  return a->operator_id < b->operator_id;
              });
    for (const NodeDefinition* node : sorted) {
        out << "node " << structural_hashes.at(node->id) << " "
            << node->operator_id << "@" << node->operator_version.ToString()
            << " frequency=" << ToString(node->frequency)
            << " residency=" << ToString(node->residency)
            << " seed=" << NodeSeedText(*node) << "\n";
        for (const auto& [name, value] : node->params) {
            out << "  param " << name << " " << ParamCanonicalText(value)
                << "\n";
        }
        std::vector<NodeInput> inputs = node->inputs;
        std::sort(inputs.begin(), inputs.end(),
                  [](const NodeInput& a, const NodeInput& b) {
                      return a.port < b.port;
                  });
        for (const auto& input : inputs) {
            out << "  input " << input.port << " "
                << structural_hashes.at(input.source_node) << "."
                << input.source_port << "\n";
        }
    }
    return out.str();
}

} // namespace

ValidationResult ValidateGraphDocument(const GraphDocument& document,
                                       const OperatorRegistry& registry) {
    ValidationResult result;
    std::vector<Diagnostic>& diagnostics = result.diagnostics;

    if (document.schema_version != kCurrentSchemaVersion) {
        if (document.schema_version < kCurrentSchemaVersion) {
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::MigrationRequired,
                "graph schema version " +
                    std::to_string(document.schema_version) +
                    " requires an explicit migration to version " +
                    std::to_string(kCurrentSchemaVersion)));
        } else {
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::UnknownSchemaVersion,
                "unknown graph schema version " +
                    std::to_string(document.schema_version)));
        }
    }
    ValidateCapabilityIdentity(document.profile, diagnostics);
    ValidateCompilerSettings(document.settings, diagnostics);

    std::map<std::string, std::size_t> node_index;
    for (std::size_t i = 0; i < document.nodes.size(); ++i) {
        const NodeDefinition& node = document.nodes[i];
        if (node.id.empty()) {
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::ParseError, "node id must not be empty"));
        }
        if (node_index.count(node.id) != 0u) {
            DiagnosticSource source;
            source.node_id = node.id;
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::DuplicateNodeId,
                "duplicate node id '" + node.id + "'", source));
        }
        node_index[node.id] = i;
    }

    for (const auto& node : document.nodes) {
        const OperatorSchema* schema =
            registry.Find(node.operator_id, node.operator_version);
        if (schema == nullptr) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            if (registry.FindAny(node.operator_id) != nullptr) {
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::UnknownOperatorVersion,
                    "unknown operator version '" + node.operator_id + "@" +
                        node.operator_version.ToString() + "'",
                    source));
            } else {
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::UnknownOperator,
                    "unknown operator '" + node.operator_id + "'", source));
            }
            continue;
        }

        if (node.frequency == Frequency::History) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::HistoryNotSupported,
                "history frequency is reserved for NW-09 and is not valid in "
                "initial graph exports",
                source));
        } else {
            bool allowed = false;
            for (Frequency frequency : schema->frequency_policy.allowed) {
                if (frequency == node.frequency) {
                    allowed = true;
                    break;
                }
            }
            if (!allowed) {
                DiagnosticSource source;
                source.node_id = node.id;
                source.operator_id = node.operator_id;
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::UnsupportedFrequency,
                    "operator '" + node.operator_id + "' does not support "
                    "frequency '" +
                        std::string(ToString(node.frequency)) + "'",
                    source));
            }
        }

        if (node.has_declared_residency &&
            node.residency != schema->PrimaryResidency()) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::InvalidResidency,
                "node '" + node.id + "' declares residency '" +
                    std::string(ToString(node.residency)) +
                    "' but operator '" + node.operator_id +
                    "' produces residency '" +
                    std::string(ToString(schema->PrimaryResidency())) + "'",
                source));
        }

        bool seed_allowed = false;
        for (SeedBehavior behavior : schema->allowed_seed_behaviors) {
            if (behavior == node.seed.behavior) {
                seed_allowed = true;
                break;
            }
        }
        if (!seed_allowed) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::UnsupportedSeedBehavior,
                "operator '" + node.operator_id +
                    "' does not support seed behavior '" +
                    std::string(ToString(node.seed.behavior)) + "'",
                source));
        }
        if (node.seed.behavior == SeedBehavior::Explicit &&
            !node.seed.has_explicit_seed) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::MissingExplicitSeed,
                "node '" + node.id +
                    "' declares an explicit seed without a value",
                source));
        }
        if (node.seed.behavior == SeedBehavior::ExplicitNodeId) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            if (node.seed.node_id.empty() ||
                node_index.count(node.seed.node_id) == 0u) {
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::InvalidSeedSource,
                    "node '" + node.id +
                        "' seeds from unknown node id '" + node.seed.node_id +
                        "'",
                    source));
            }
        }
        if (node.seed.behavior == SeedBehavior::DerivedFromUpstream &&
            node.inputs.empty()) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::InvalidSeedSource,
                "node '" + node.id +
                    "' requests upstream-derived seed without inputs",
                source));
        }

        ValidateParams(node, *schema, diagnostics);

        std::set<std::string> seen_input_ports;
        for (const auto& input : node.inputs) {
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            source.port_id = input.port;
            source.source_node_id = input.source_node;
            source.source_port_id = input.source_port;

            const PortSpec* input_port = schema->FindInput(input.port);
            if (input_port == nullptr) {
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::UnknownInputPort,
                    "operator '" + node.operator_id + "' has no input port '" +
                        input.port + "'",
                    source));
                continue;
            }
            if (!seen_input_ports.insert(input.port).second) {
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::DuplicateInputPort,
                    "input port '" + input.port +
                        "' is connected more than once on node '" + node.id +
                        "'",
                    source));
            }
            const auto source_it = node_index.find(input.source_node);
            if (source_it == node_index.end()) {
                diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::UnknownInputNode,
                    "node '" + node.id + "' port '" + input.port +
                        "' references unknown source node '" +
                        input.source_node + "'",
                    source));
                continue;
            }
            const NodeDefinition& source_node =
                document.nodes[source_it->second];
            const OperatorSchema* source_schema = registry.Find(
                source_node.operator_id, source_node.operator_version);
            if (source_schema == nullptr) {
                source_schema = registry.FindAny(source_node.operator_id);
            }
            if (source_schema == nullptr) {
                continue;
            }
            const PortSpec* output_port =
                source_schema->FindOutput(input.source_port);
            if (output_port == nullptr) {
                Diagnostic diagnostic = MakeDiagnostic(
                    DiagnosticCode::UnknownOutputPort,
                    "source node '" + input.source_node +
                        "' has no output port '" + input.source_port + "'",
                    source);
                diagnostic.dependency_path = {
                    input.source_node + "." + input.source_port + " -> " +
                    node.id + "." + input.port};
                diagnostics.push_back(std::move(diagnostic));
                continue;
            }
            if (input_port->type != output_port->type) {
                Diagnostic diagnostic = MakeDiagnostic(
                    DiagnosticCode::PortTypeMismatch,
                    "input '" + node.id + "." + input.port + "' expects " +
                        std::string(ToString(input_port->type)) +
                        " but source '" + input.source_node + "." +
                        input.source_port + "' provides " +
                        std::string(ToString(output_port->type)),
                    source);
                diagnostic.dependency_path = {
                    input.source_node + "." + input.source_port + " -> " +
                    node.id + "." + input.port};
                diagnostics.push_back(std::move(diagnostic));
                continue;
            }
            if ((input_port->type == LogicalType::Vector ||
                 input_port->type == LogicalType::Matrix) &&
                input_port->dimension != output_port->dimension) {
                Diagnostic diagnostic = MakeDiagnostic(
                    DiagnosticCode::PortDimensionMismatch,
                    "input '" + node.id + "." + input.port + "' expects " +
                        std::to_string(input_port->dimension) +
                        " components but source '" + input.source_node + "." +
                        input.source_port + "' provides " +
                        std::to_string(output_port->dimension),
                    source);
                diagnostic.dependency_path = {
                    input.source_node + "." + input.source_port + " -> " +
                    node.id + "." + input.port};
                diagnostics.push_back(std::move(diagnostic));
                continue;
            }
            if (input_port->residency != output_port->residency) {
                Diagnostic diagnostic;
                if (output_port->residency == Residency::Gpu &&
                    input_port->residency == Residency::Cpu) {
                    diagnostic = MakeDiagnostic(
                        DiagnosticCode::ImplicitReadback,
                        "input '" + node.id + "." + input.port +
                            "' requires CPU residency but source '" +
                            input.source_node + "." + input.source_port +
                            "' is GPU-resident; insert an explicit readback",
                        source);
                } else if (output_port->residency != Residency::Gpu &&
                           input_port->residency == Residency::Gpu) {
                    diagnostic = MakeDiagnostic(
                        DiagnosticCode::ImplicitUpload,
                        "input '" + node.id + "." + input.port +
                            "' requires GPU residency but source '" +
                            input.source_node + "." + input.source_port +
                            "' is not GPU-resident; insert an explicit "
                            "conversion",
                        source);
                } else {
                    diagnostic = MakeDiagnostic(
                        DiagnosticCode::InvalidResidency,
                        "input '" + node.id + "." + input.port +
                            "' residency '" +
                            std::string(ToString(input_port->residency)) +
                            "' is incompatible with source '" +
                            input.source_node + "." + input.source_port +
                            "' residency '" +
                            std::string(ToString(output_port->residency)) +
                            "'",
                        source);
                }
                diagnostic.dependency_path = {
                    input.source_node + "." + input.source_port + " -> " +
                    node.id + "." + input.port};
                diagnostics.push_back(std::move(diagnostic));
            }
            if (static_cast<int>(source_node.frequency) >
                static_cast<int>(node.frequency)) {
                Diagnostic diagnostic = MakeDiagnostic(
                    DiagnosticCode::FrequencyTransition,
                    "node '" + node.id + "' runs at frequency '" +
                        std::string(ToString(node.frequency)) +
                        "' but consumes '" + input.source_node + "." +
                        input.source_port + "' produced at faster frequency '" +
                        std::string(ToString(source_node.frequency)) + "'",
                    source);
                diagnostic.dependency_path = {
                    input.source_node + "." + input.source_port + " -> " +
                    node.id + "." + input.port};
                diagnostics.push_back(std::move(diagnostic));
            }
        }
        for (const auto& port : schema->inputs) {
            if (seen_input_ports.count(port.id) != 0u) {
                continue;
            }
            DiagnosticSource source;
            source.node_id = node.id;
            source.operator_id = node.operator_id;
            source.port_id = port.id;
            diagnostics.push_back(MakeDiagnostic(
                DiagnosticCode::MissingInputPort,
                "node '" + node.id + "' is missing a connection for input "
                "port '" + port.id + "'",
                source));
        }
    }

    TopologyResult topology = ComputeTopology(document);
    diagnostics.insert(diagnostics.end(), topology.diagnostics.begin(),
                       topology.diagnostics.end());

    if (HasErrors(diagnostics) || !topology.ok) {
        return result;
    }

    GraphDocument normalized = document;
    FillDefaultParams(normalized, registry);

    std::map<std::string, std::string> structural_hashes;
    for (const std::string& node_id : topology.order) {
        const auto index_it = node_index.find(node_id);
        if (index_it == node_index.end()) {
            continue;
        }
        const NodeDefinition& node = normalized.nodes[index_it->second];
        const OperatorSchema* schema =
            registry.Find(node.operator_id, node.operator_version);
        if (schema == nullptr) {
            continue;
        }
        CanonicalWriter writer;
        writer.String("nwgraph.node.v1");
        writer.String(node.operator_id);
        writer.Version(node.operator_version);
        writer.U64(static_cast<std::uint64_t>(node.frequency));
        writer.String(OperatorContractDigest(*schema));
        writer.String(ToString(node.seed.behavior));
        writer.Bool(node.seed.has_explicit_seed);
        writer.U64(node.seed.explicit_seed);
        writer.String(node.seed.behavior == SeedBehavior::ExplicitNodeId
                          ? node.seed.node_id
                          : std::string());
        writer.MapHeader(static_cast<std::uint32_t>(node.params.size()));
        for (const auto& [name, value] : node.params) {
            writer.String(name);
            EncodeParam(writer, value);
        }
        std::vector<NodeInput> inputs = node.inputs;
        std::sort(inputs.begin(), inputs.end(),
                  [](const NodeInput& a, const NodeInput& b) {
                      return a.port < b.port;
                  });
        writer.ListHeader(static_cast<std::uint32_t>(inputs.size()));
        for (const auto& input : inputs) {
            const auto upstream = structural_hashes.find(input.source_node);
            writer.String(input.port);
            writer.String(upstream == structural_hashes.end()
                              ? std::string()
                              : upstream->second);
            writer.String(input.source_port);
        }
        structural_hashes[node_id] = RawDigestHex(writer.bytes());
    }

    GraphSnapshot snapshot;
    snapshot.schema_version_ = normalized.schema_version;
    snapshot.profile_ = normalized.profile;
    snapshot.settings_ = normalized.settings;
    snapshot.nodes_ = normalized.nodes;
    snapshot.topological_order_ = topology.order;
    for (std::size_t i = 0; i < snapshot.nodes_.size(); ++i) {
        snapshot.index_by_id_[snapshot.nodes_[i].id] = i;
    }
    snapshot.structural_hashes_ = structural_hashes;
    snapshot.canonical_bytes_ =
        BuildCanonicalSemanticBytes(normalized, structural_hashes);
    snapshot.semantic_digest_ =
        "sha256:v1:" +
        RawDigestHex(snapshot.canonical_bytes_);
    snapshot.canonical_text_ =
        BuildCanonicalText(normalized, structural_hashes);
    result.snapshot = std::move(snapshot);
    return result;
}

const NodeDefinition* GraphSnapshot::FindNode(std::string_view id) const {
    const auto found =
        index_by_id_.find(std::string(id.data(), id.size()));
    if (found == index_by_id_.end()) {
        return nullptr;
    }
    return &nodes_[found->second];
}

std::string GraphSnapshot::StructuralHash(std::string_view node_id) const {
    const auto found =
        structural_hashes_.find(std::string(node_id.data(), node_id.size()));
    return found == structural_hashes_.end() ? std::string() : found->second;
}

MigrationResult MigrateGraphDocument(const GraphDocument& document,
                                     const OperatorRegistry& registry,
                                     std::uint32_t target_version) {
    MigrationResult result;
    if (document.schema_version == target_version) {
        result.document = document;
        return result;
    }
    if (document.schema_version > kCurrentSchemaVersion ||
        target_version != kCurrentSchemaVersion) {
        result.diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::UnsupportedMigration,
            "no explicit migration path from schema version " +
                std::to_string(document.schema_version) + " to " +
                std::to_string(target_version)));
        return result;
    }
    if (document.schema_version != 0u) {
        result.diagnostics.push_back(MakeDiagnostic(
            DiagnosticCode::UnsupportedMigration,
            "no explicit migration path from schema version " +
                std::to_string(document.schema_version) + " to " +
                std::to_string(target_version)));
        return result;
    }

    GraphDocument migrated = document;
    migrated.schema_version = kCurrentSchemaVersion;
    for (auto& node : migrated.nodes) {
        if (node.operator_version == SemanticVersion{}) {
            const OperatorSchema* schema = registry.FindAny(node.operator_id);
            if (schema == nullptr) {
                DiagnosticSource source;
                source.node_id = node.id;
                source.operator_id = node.operator_id;
                result.diagnostics.push_back(MakeDiagnostic(
                    DiagnosticCode::UnknownOperator,
                    "legacy node '" + node.id + "' references unknown operator '" +
                        node.operator_id + "'",
                    source));
                continue;
            }
            node.operator_version = schema->version;
        }
        const OperatorSchema* schema =
            registry.Find(node.operator_id, node.operator_version);
        if (schema != nullptr) {
            if (node.frequency == Frequency::Frame &&
                schema->frequency_policy.default_frequency != Frequency::Frame) {
                node.frequency = schema->frequency_policy.default_frequency;
            }
            node.residency = schema->PrimaryResidency();
            if (node.seed.behavior == SeedBehavior::None &&
                schema->default_seed_behavior != SeedBehavior::None) {
                node.seed.behavior = schema->default_seed_behavior;
            }
        }
    }
    if (HasErrors(result.diagnostics)) {
        return result;
    }
    result.document = std::move(migrated);
    return result;
}

std::vector<MigrationDescriptor> AvailableMigrations() {
    return {MigrationDescriptor{
        0u, 1u, "nwgraph-v0-to-v1",
        "Add explicit operator semantic versions and current frequency "
        "defaults to legacy positional node declarations."}};
}

std::string EmitDocumentText(const GraphDocument& document) {
    std::ostringstream out;
    out << "nwgraph " << document.schema_version << "\n";
    out << "profile " << document.profile.id << " "
        << document.profile.requirements_digest << "\n";
    out << "setting optimization " << document.settings.optimization << "\n";
    out << "setting deterministic "
        << (document.settings.deterministic ? "true" : "false") << "\n";
    out << "setting api " << document.settings.api << "\n";
    out << "setting feature_level " << document.settings.feature_level << "\n";
    out << "setting shader_model " << document.settings.shader_model << "\n";
    out << "setting frequency_policy " << document.settings.frequency_policy
        << "\n";
    out << "setting shader_blob_policy "
        << document.settings.shader_blob_policy << "\n";
    out << "setting quantization_policy "
        << document.settings.quantization.policy << "\n";
    out << "setting quantization_bits " << document.settings.quantization.bits
        << "\n";
    out << "setting quantization_step "
        << CanonicalNumberText(document.settings.quantization.step) << "\n";

    std::vector<const NodeDefinition*> sorted;
    for (const auto& node : document.nodes) {
        sorted.push_back(&node);
    }
    std::sort(sorted.begin(), sorted.end(),
              [](const NodeDefinition* a, const NodeDefinition* b) {
                  return a->id < b->id;
              });
    for (const NodeDefinition* node : sorted) {
        out << "node " << node->id << " " << node->operator_id << "@"
            << node->operator_version.ToString()
            << " frequency=" << ToString(node->frequency);
        if (node->has_declared_residency) {
            out << " residency=" << ToString(node->residency);
        }
        out << "\n";
        for (const auto& [name, value] : node->params) {
            out << "  param " << name << " " << ParamCanonicalText(value)
                << "\n";
        }
        if (node->seed.behavior != SeedBehavior::None) {
            if (node->seed.behavior == SeedBehavior::Explicit) {
                out << "  seed explicit " << node->seed.explicit_seed << "\n";
            } else if (node->seed.behavior == SeedBehavior::ExplicitNodeId) {
                out << "  seed node-id " << node->seed.node_id << "\n";
            } else {
                out << "  seed " << ToString(node->seed.behavior) << "\n";
            }
        }
        std::vector<NodeInput> inputs = node->inputs;
        std::sort(inputs.begin(), inputs.end(),
                  [](const NodeInput& a, const NodeInput& b) {
                      return a.port < b.port;
                  });
        for (const auto& input : inputs) {
            out << "  input " << input.port << " " << input.source_node << "."
                << input.source_port << "\n";
        }
    }
    for (const auto& [node_id, layout] : document.layout.nodes) {
        out << "layout " << node_id << " " << CanonicalNumberText(layout.x)
            << " " << CanonicalNumberText(layout.y);
        if (layout.collapsed) {
            out << " collapsed=true";
        }
        out << "\n";
    }
    return out.str();
}

// ---------------------------------------------------------------------------
// CPU reference evaluation and deterministic memoization
// ---------------------------------------------------------------------------

namespace {

std::string NumberBitsHex(double value) {
    if (!std::isfinite(value)) {
        value = 0.0;
    }
    value = NormalizeZero(value);
    std::uint64_t bits = 0;
    std::memcpy(&bits, &value, sizeof(bits));
    std::uint8_t bytes[8]{};
    for (int i = 7; i >= 0; --i) {
        bytes[7 - i] = static_cast<std::uint8_t>(
            (bits >> (static_cast<unsigned>(i) * 8u)) & 0xffu);
    }
    return HexBytes(bytes, 8u);
}

std::string CanonicalEvalNumber(double value) {
    return "f64:" + NumberBitsHex(value);
}

void EncodeEvaluationValue(CanonicalWriter& writer,
                           const EvaluationValue& value) {
    writer.String(ToString(value.type));
    writer.String(ToString(value.residency));
    writer.U64(value.component_count);
    for (std::uint32_t i = 0; i < value.component_count; ++i) {
        writer.F64(value.numbers[i]);
    }
    writer.String(value.text);
    writer.ListHeader(static_cast<std::uint32_t>(value.samples.size()));
    for (double sample : value.samples) {
        writer.F64(sample);
    }
}

EvaluationValue MakeScalar(double value) {
    EvaluationValue out;
    out.type = LogicalType::Scalar;
    out.residency = Residency::Cpu;
    out.component_count = 1u;
    out.numbers[0] = NormalizeZero(value);
    return out;
}

EvaluationValue MakeVector(std::initializer_list<double> components,
                           LogicalType type = LogicalType::Vector,
                           Residency residency = Residency::Cpu) {
    EvaluationValue out;
    out.type = type;
    out.residency = residency;
    out.component_count = static_cast<std::uint32_t>(components.size());
    std::uint32_t index = 0;
    for (double value : components) {
        if (index < out.numbers.size()) {
            out.numbers[index] = NormalizeZero(value);
        }
        ++index;
    }
    return out;
}

EvaluationValue MakeText(LogicalType type, std::string text,
                         Residency residency = Residency::Cpu) {
    EvaluationValue out;
    out.type = type;
    out.residency = residency;
    out.text = std::move(text);
    return out;
}

double UnitFromU64(std::uint64_t value) {
    return static_cast<double>(value >> 11u) *
           (1.0 / 9007199254740992.0);
}

double UnitFromBytes(const std::array<std::uint8_t, 32>& digest,
                     std::size_t offset = 0u) {
    std::uint64_t value = 0;
    for (std::size_t i = 0; i < 8u; ++i) {
        value = (value << 8u) | digest[(offset + i) % digest.size()];
    }
    return UnitFromU64(value);
}

std::uint32_t HashU32(std::uint32_t a, std::uint32_t b, std::uint32_t c) {
    std::uint32_t h = a * 0x9e3779b1u;
    h ^= b + 0x85ebca6bu + (h << 6u) + (h >> 2u);
    h ^= c + 0xc2b2ae35u + (h << 6u) + (h >> 2u);
    h ^= h >> 16u;
    h *= 0x7feb352du;
    h ^= h >> 15u;
    h *= 0x846ca68bu;
    h ^= h >> 16u;
    return h;
}

const EvaluationValue* FindInputValue(
    const std::map<std::string, OutputValues>& evaluated,
    const NodeInput& input, const std::string& port) {
    const auto node_it = evaluated.find(input.source_node);
    if (node_it == evaluated.end()) {
        return nullptr;
    }
    const auto value_it = node_it->second.find(input.source_port);
    if (value_it == node_it->second.end()) {
        return nullptr;
    }
    (void)port;
    return &value_it->second;
}

EvaluationValue ImageOracle(const GraphSnapshot& snapshot,
                            const NodeDefinition& node,
                            const std::string& output_port,
                            const std::map<std::string, OutputValues>& evaluated) {
    CanonicalWriter writer;
    writer.String("nwgraph.image-oracle.v1");
    writer.String(snapshot.StructuralHash(node.id));
    writer.String(output_port);
    std::vector<NodeInput> inputs = node.inputs;
    std::sort(inputs.begin(), inputs.end(),
              [](const NodeInput& a, const NodeInput& b) {
                  return a.port < b.port;
              });
    writer.MapHeader(static_cast<std::uint32_t>(inputs.size()));
    for (const auto& input : inputs) {
        writer.String(input.port);
        const auto node_it = evaluated.find(input.source_node);
        if (node_it != evaluated.end()) {
            const auto value_it = node_it->second.find(input.source_port);
            if (value_it != node_it->second.end()) {
                EncodeEvaluationValue(writer, value_it->second);
                continue;
            }
        }
        writer.String("<missing>");
    }
    const auto digest = Sha256Bytes(writer.bytes().data(),
                                    writer.bytes().size());
    EvaluationValue image = MakeVector(
        {UnitFromBytes(digest, 0u), UnitFromBytes(digest, 8u),
         UnitFromBytes(digest, 16u), UnitFromBytes(digest, 24u)},
        LogicalType::RenderImage, Residency::Gpu);
    image.text = "image-oracle:sha256:v1:" +
                 HexBytes(digest.data(), digest.size());
    return image;
}

std::vector<std::pair<double, double>> ParseCurveKeys(const std::string& text) {
    std::vector<std::pair<double, double>> keys;
    std::size_t start = 0;
    while (start <= text.size()) {
        const std::size_t end = text.find(';', start);
        const std::string_view entry =
            std::string_view(text.data(), text.size()).substr(
                start, end == std::string::npos ? std::string::npos
                                                : end - start);
        if (!entry.empty()) {
            const std::size_t colon = entry.find(':');
            if (colon != std::string_view::npos) {
                double time = 0.0;
                double value = 0.0;
                if (ParseDouble(entry.substr(0, colon), time) &&
                    ParseDouble(entry.substr(colon + 1u), value) &&
                    std::isfinite(time) && std::isfinite(value)) {
                    keys.emplace_back(time, value);
                }
            }
        }
        if (end == std::string::npos) {
            break;
        }
        start = end + 1u;
    }
    std::sort(keys.begin(), keys.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });
    return keys;
}

double SampleCurve(const std::vector<double>& samples, double t, bool step) {
    if (samples.size() < 2u) {
        return 0.0;
    }
    const std::size_t count = samples.size() / 2u;
    if (t <= samples[0]) {
        return samples[1];
    }
    for (std::size_t i = 1; i < count; ++i) {
        const double t0 = samples[(i - 1u) * 2u];
        const double v0 = samples[(i - 1u) * 2u + 1u];
        const double t1 = samples[i * 2u];
        const double v1 = samples[i * 2u + 1u];
        if (t <= t1) {
            if (step || t1 == t0) {
                return v0;
            }
            const double alpha = (t - t0) / (t1 - t0);
            return v0 + (v1 - v0) * alpha;
        }
    }
    return samples[count * 2u - 1u];
}

Diagnostic EvalDiagnostic(const NodeDefinition& node,
                          const std::string& message) {
    DiagnosticSource source;
    source.node_id = node.id;
    source.operator_id = node.operator_id;
    return MakeDiagnostic(DiagnosticCode::EvaluationFailure, message, source);
}

bool EvaluateNode(const GraphSnapshot& snapshot, const NodeDefinition& node,
                  const std::map<std::string, OutputValues>& evaluated,
                  const EvaluationContext& context, OutputValues& outputs,
                  Diagnostic& failure) {
    const auto param = [&](std::string_view name) -> const ParamValue* {
        const auto found =
            node.params.find(std::string(name.data(), name.size()));
        return found == node.params.end() ? nullptr : &found->second;
    };
    const auto input = [&](std::string_view port) -> const EvaluationValue* {
        for (const auto& connection : node.inputs) {
            if (std::string_view(connection.port) == port) {
                return FindInputValue(
                    evaluated, connection,
                    std::string(port.data(), port.size()));
            }
        }
        return nullptr;
    };

    const std::string& op = node.operator_id;
    if (op == "nw.math.scalar_const") {
        outputs["out"] = MakeScalar(param("value")->number);
        return true;
    }
    if (op == "nw.math.vec2_const" || op == "nw.math.vec3_const" ||
        op == "nw.math.vec4_const") {
        const ParamValue* value = param("value");
        EvaluationValue out;
        out.type = LogicalType::Vector;
        out.residency = Residency::Cpu;
        out.component_count = value->component_count;
        for (std::uint32_t i = 0; i < value->component_count; ++i) {
            out.numbers[i] = value->components[i];
        }
        outputs["out"] = out;
        return true;
    }
    if (op == "nw.math.mat4_const") {
        const ParamValue* value = param("value");
        EvaluationValue out;
        out.type = LogicalType::Matrix;
        out.residency = Residency::Cpu;
        out.component_count = value->component_count;
        for (std::uint32_t i = 0; i < value->component_count; ++i) {
            out.numbers[i] = value->components[i];
        }
        outputs["out"] = out;
        return true;
    }
    if (op == "nw.math.add") {
        outputs["out"] = MakeScalar(input("a")->numbers[0] +
                                    input("b")->numbers[0]);
        return true;
    }
    if (op == "nw.math.mul") {
        outputs["out"] = MakeScalar(input("a")->numbers[0] *
                                    input("b")->numbers[0]);
        return true;
    }
    if (op == "nw.math.sin") {
        outputs["out"] = MakeScalar(std::sin(input("x")->numbers[0]));
        return true;
    }
    if (op == "nw.math.clamp01") {
        outputs["out"] = MakeScalar(
            std::clamp(input("x")->numbers[0], 0.0, 1.0));
        return true;
    }
    if (op == "nw.math.vec4_scale") {
        const EvaluationValue* value = input("v");
        const double scale = input("s")->numbers[0];
        outputs["out"] = MakeVector({value->numbers[0] * scale,
                                     value->numbers[1] * scale,
                                     value->numbers[2] * scale,
                                     value->numbers[3] * scale});
        return true;
    }
    if (op == "nw.math.vec4_split") {
        const EvaluationValue* value = input("v");
        outputs["x"] = MakeScalar(value->numbers[0]);
        outputs["y"] = MakeScalar(value->numbers[1]);
        outputs["z"] = MakeScalar(value->numbers[2]);
        outputs["w"] = MakeScalar(value->numbers[3]);
        return true;
    }
    if (op == "nw.math.dot") {
        const EvaluationValue* a = input("a");
        const EvaluationValue* b = input("b");
        outputs["out"] = MakeScalar(a->numbers[0] * b->numbers[0] +
                                    a->numbers[1] * b->numbers[1] +
                                    a->numbers[2] * b->numbers[2]);
        return true;
    }
    if (op == "nw.control.smoothstep") {
        const double e0 = input("edge0")->numbers[0];
        const double e1 = input("edge1")->numbers[0];
        const double x = input("x")->numbers[0];
        double t = (e1 == e0) ? (x < e0 ? 0.0 : 1.0) : (x - e0) / (e1 - e0);
        t = std::clamp(t, 0.0, 1.0);
        outputs["out"] = MakeScalar(t * t * (3.0 - 2.0 * t));
        return true;
    }
    if (op == "nw.control.seed") {
        outputs["out"] = MakeScalar(UnitFromU64(DeriveSeed(snapshot, node,
                                                           context)));
        return true;
    }
    if (op == "nw.control.image_cpu_passthrough") {
        outputs["out"] = *input("image");
        outputs["out"].residency = Residency::Cpu;
        return true;
    }
    if (op == "nw.proc.hash_noise") {
        CanonicalWriter writer;
        writer.String("nwgraph.hash-noise.v1");
        writer.String(snapshot.StructuralHash(node.id));
        writer.U64(DeriveSeed(snapshot, node, context));
        writer.F64(input("seed")->numbers[0]);
        const auto digest = Sha256Bytes(writer.bytes().data(),
                                        writer.bytes().size());
        outputs["out"] = MakeScalar(UnitFromBytes(digest, 0u));
        return true;
    }
    if (op == "nw.proc.value_noise") {
        const EvaluationValue* position = input("position");
        const std::uint32_t x = static_cast<std::uint32_t>(
            static_cast<std::int64_t>(std::floor(position->numbers[0] * 16.0)));
        const std::uint32_t y = static_cast<std::uint32_t>(
            static_cast<std::int64_t>(std::floor(position->numbers[1] * 16.0)));
        const std::uint32_t z = static_cast<std::uint32_t>(
            static_cast<std::int64_t>(std::floor(position->numbers[2] * 16.0)));
        const std::uint32_t hash = HashU32(
            x ^ static_cast<std::uint32_t>(DeriveSeed(snapshot, node, context)),
            y, z);
        outputs["out"] = MakeScalar(static_cast<double>(hash) /
                                    4294967296.0);
        return true;
    }
    if (op == "nw.proc.checker") {
        const EvaluationValue* uv = input("uv");
        const double scale = input("scale")->numbers[0];
        const auto cx = static_cast<std::int64_t>(
            std::floor(uv->numbers[0] * scale));
        const auto cy = static_cast<std::int64_t>(
            std::floor(uv->numbers[1] * scale));
        outputs["out"] = MakeScalar(((cx + cy) & 1ll) == 0ll ? 0.0 : 1.0);
        return true;
    }
    if (op == "nw.proc.palette") {
        const double t = input("t")->numbers[0];
        outputs["out"] = MakeVector(
            {0.5 + 0.5 * std::cos(6.283185307179586 * (t + 0.00)),
             0.5 + 0.5 * std::cos(6.283185307179586 * (t + 0.33)),
             0.5 + 0.5 * std::cos(6.283185307179586 * (t + 0.67))});
        return true;
    }
    if (op == "nw.curve.keyframes") {
        EvaluationValue curve;
        curve.type = LogicalType::Curve;
        curve.residency = Residency::Cpu;
        const auto keys = ParseCurveKeys(param("keys")->text);
        curve.numbers[0] = static_cast<double>(keys.size());
        curve.component_count = 1u;
        for (const auto& [time, value] : keys) {
            curve.samples.push_back(time);
            curve.samples.push_back(value);
        }
        curve.text = param("keys")->text;
        outputs["out"] = std::move(curve);
        return true;
    }
    if (op == "nw.curve.sample") {
        const EvaluationValue* curve = input("curve");
        const bool step = false;
        outputs["out"] = MakeScalar(
            SampleCurve(curve->samples, input("t")->numbers[0], step));
        return true;
    }
    if (op == "nw.scene.flat") {
        const std::string& entries = param("entries")->text;
        std::size_t count = 0;
        if (!entries.empty()) {
            count = 1u;
            for (char c : entries) {
                if (c == ';') {
                    ++count;
                }
            }
        }
        EvaluationValue scene = MakeText(LogicalType::SceneTable, entries);
        scene.component_count = 1u;
        scene.numbers[0] = static_cast<double>(count);
        outputs["out"] = std::move(scene);
        return true;
    }
    if (op == "nw.camera.perspective") {
        EvaluationValue camera = MakeText(LogicalType::Camera,
                                          "camera:perspective");
        camera.component_count = 4u;
        camera.numbers[0] = param("fov_degrees")->number;
        camera.numbers[1] = param("aspect")->number;
        camera.numbers[2] = param("near")->number;
        camera.numbers[3] = param("far")->number;
        outputs["out"] = std::move(camera);
        return true;
    }
    if (op == "nw.light.directional") {
        const ParamValue* direction = param("direction");
        const ParamValue* color = param("color");
        EvaluationValue light = MakeText(LogicalType::Light,
                                         "light:directional");
        light.component_count = 6u;
        for (std::uint32_t i = 0; i < 3u; ++i) {
            light.numbers[i] = direction->components[i];
            light.numbers[3u + i] = color->components[i];
        }
        outputs["out"] = std::move(light);
        return true;
    }
    if (op == "nw.material.describe") {
        const EvaluationValue* color = input("base_color");
        EvaluationValue material = MakeText(LogicalType::Material,
                                            "material:pbr");
        material.component_count = 7u;
        for (std::uint32_t i = 0; i < 4u; ++i) {
            material.numbers[i] = color->numbers[i];
        }
        material.numbers[4] = input("roughness")->numbers[0];
        material.numbers[5] = param("metallic")->number;
        material.numbers[6] = param("specular")->number;
        outputs["out"] = std::move(material);
        return true;
    }
    if (op == "nw.shader.vec4_const") {
        const ParamValue* value = param("value");
        std::ostringstream text;
        text << "vec4(";
        for (std::uint32_t i = 0; i < value->component_count; ++i) {
            if (i != 0) text << ", ";
            text << CanonicalNumberText(value->components[i]);
        }
        text << ")";
        outputs["out"] =
            MakeText(LogicalType::ShaderExpr, text.str(), Residency::Logical);
        return true;
    }
    if (op == "nw.shader.expr") {
        outputs["out"] = MakeText(LogicalType::ShaderExpr,
                                  param("expression")->text,
                                  Residency::Logical);
        return true;
    }
    if (op == "nw.shader.add" || op == "nw.shader.mul") {
        const std::string symbol = op == "nw.shader.add" ? " + " : " * ";
        outputs["out"] = MakeText(
            LogicalType::ShaderExpr,
            "(" + input("a")->text + symbol + input("b")->text + ")",
            Residency::Logical);
        return true;
    }
    if (op == "nw.shader.mix") {
        outputs["out"] = MakeText(
            LogicalType::ShaderExpr,
            "mix(" + input("a")->text + ", " + input("b")->text + ", " +
                CanonicalNumberText(param("factor")->number) + ")",
            Residency::Logical);
        return true;
    }
    if (op == "nw.texture.procedural") {
        CanonicalWriter writer;
        writer.String("nwgraph.texture-recipe.v1");
        writer.String(snapshot.StructuralHash(node.id));
        writer.U64(static_cast<std::uint64_t>(param("seed")->integer));
        writer.U64(static_cast<std::uint64_t>(param("width")->integer));
        writer.U64(static_cast<std::uint64_t>(param("height")->integer));
        writer.String(param("format")->text);
        const auto digest = Sha256Bytes(writer.bytes().data(),
                                        writer.bytes().size());
        EvaluationValue texture = MakeText(
            LogicalType::TextureRecipe,
            "texture-recipe:sha256:v1:" + HexBytes(digest.data(),
                                                    digest.size()),
            Residency::Logical);
        texture.component_count = 3u;
        texture.numbers[0] = static_cast<double>(param("width")->integer);
        texture.numbers[1] = static_cast<double>(param("height")->integer);
        texture.numbers[2] = static_cast<double>(param("seed")->integer);
        outputs["out"] = std::move(texture);
        return true;
    }
    if (op == "nw.buffer.recipe") {
        EvaluationValue buffer = MakeText(
            LogicalType::BufferRecipe,
            "buffer-recipe:" + param("format")->text, Residency::Logical);
        buffer.component_count = 1u;
        buffer.numbers[0] = static_cast<double>(param("bytes")->integer);
        outputs["out"] = std::move(buffer);
        return true;
    }
    if (op == "nw.mesh.recipe") {
        EvaluationValue mesh = MakeText(LogicalType::MeshRecipe,
                                        "mesh-recipe:procedural",
                                        Residency::Logical);
        mesh.component_count = 2u;
        mesh.numbers[0] = static_cast<double>(param("vertices")->integer);
        mesh.numbers[1] = static_cast<double>(param("indices")->integer);
        outputs["out"] = std::move(mesh);
        return true;
    }
    if (op == "nw.residency.upload_texture" ||
        op == "nw.residency.upload_buffer" ||
        op == "nw.residency.upload_mesh") {
        const char* port = op == "nw.residency.upload_texture"
                               ? "texture"
                               : op == "nw.residency.upload_buffer" ? "buffer"
                                                                    : "mesh";
        outputs["out"] = *input(port);
        outputs["out"].residency = Residency::Gpu;
        return true;
    }
    if (op == "nw.residency.gpu_to_cpu" ||
        op == "nw.residency.cpu_to_gpu") {
        outputs["out"] = *input("image");
        outputs["out"].residency =
            op == "nw.residency.gpu_to_cpu" ? Residency::Cpu : Residency::Gpu;
        return true;
    }
    if (op == "nw.readback.scalar") {
        const EvaluationValue* image = input("image");
        outputs["value"] = MakeScalar(image->numbers[0]);
        return true;
    }
    if (op == "nw.render.fullscreen") {
        outputs["image"] = ImageOracle(snapshot, node, "image", evaluated);
        return true;
    }
    if (op == "nw.render.gbuffer") {
        outputs["color"] = ImageOracle(snapshot, node, "color", evaluated);
        outputs["normals"] = ImageOracle(snapshot, node, "normals", evaluated);
        return true;
    }
    if (op == "nw.render.composite") {
        outputs["out"] = ImageOracle(snapshot, node, "out", evaluated);
        return true;
    }
    if (op == "nw.render.present") {
        outputs["out"] = ImageOracle(snapshot, node, "out", evaluated);
        return true;
    }

    failure = EvalDiagnostic(node,
                             "no CPU reference implementation for operator '" +
                                 op + "'");
    return false;
}

} // namespace

std::string EvaluationValue::CanonicalText() const {
    std::ostringstream out;
    out << "type=" << ToString(type) << ";residency=" << ToString(residency)
        << ";";
    if (component_count == 0u) {
        out << "components=0";
    } else {
        out << "components=" << component_count << ";values=";
        for (std::uint32_t i = 0; i < component_count; ++i) {
            if (i != 0) {
                out << ",";
            }
            out << CanonicalEvalNumber(numbers[i]);
        }
    }
    if (!text.empty()) {
        out << ";text=" << text;
    }
    if (!samples.empty()) {
        out << ";samples=";
        for (std::size_t i = 0; i < samples.size(); ++i) {
            if (i != 0) {
                out << ",";
            }
            out << CanonicalEvalNumber(samples[i]);
        }
    }
    return out.str();
}

std::uint64_t DeriveSeed(const GraphSnapshot& snapshot,
                         const NodeDefinition& node,
                         const EvaluationContext& context) {
    if (node.seed.behavior == SeedBehavior::None) {
        return 0u;
    }
    if (node.seed.behavior == SeedBehavior::Explicit) {
        return node.seed.explicit_seed;
    }
    CanonicalWriter writer;
    writer.String("nwgraph.seed.v1");
    writer.String(snapshot.semantic_digest());
    writer.String(snapshot.StructuralHash(node.id));
    writer.String(ToString(node.seed.behavior));
    writer.U64(node.seed.explicit_seed);
    writer.String(node.seed.behavior == SeedBehavior::ExplicitNodeId
                      ? node.seed.node_id
                      : std::string());
    writer.U64(context.seed_base);
    const auto digest = Sha256Bytes(writer.bytes().data(),
                                    writer.bytes().size());
    std::uint64_t result = 0;
    for (std::size_t i = 0; i < 8u; ++i) {
        result = (result << 8u) | digest[i];
    }
    return result;
}

GraphEvaluator::GraphEvaluator(const GraphSnapshot& snapshot)
    : snapshot_(snapshot) {
    for (const auto& node : snapshot_.nodes()) {
        for (const auto& input : node.inputs) {
            auto& dependents = dependents_[input.source_node];
            if (std::find(dependents.begin(), dependents.end(), node.id) ==
                dependents.end()) {
                dependents.push_back(node.id);
            }
        }
    }
}

void GraphEvaluator::ApplyInputChange(const std::string& node_id) {
    std::set<std::string> closure;
    std::vector<std::string> stack{node_id};
    while (!stack.empty()) {
        const std::string current = stack.back();
        stack.pop_back();
        if (!closure.insert(current).second) {
            continue;
        }
        const auto found = dependents_.find(current);
        if (found != dependents_.end()) {
            for (const auto& dependent : found->second) {
                stack.push_back(dependent);
            }
        }
    }
    for (const auto& id : closure) {
        const auto found = entries_.find(id);
        if (found == entries_.end()) {
            continue;
        }
        for (auto& entry : found->second) {
            entry.invalid = true;
        }
    }
}

void GraphEvaluator::CorruptCacheEntry(const std::string& node_id) {
    const auto found = entries_.find(node_id);
    if (found == entries_.end()) {
        return;
    }
    for (auto& entry : found->second) {
        entry.corrupt = true;
    }
}

bool GraphEvaluator::HasValidCacheEntry(const std::string& node_id) const {
    const auto found = entries_.find(node_id);
    if (found == entries_.end()) {
        return false;
    }
    for (const auto& entry : found->second) {
        if (!entry.invalid && !entry.corrupt) {
            return true;
        }
    }
    return false;
}

EvaluationResult GraphEvaluator::Evaluate(const EvaluationContext& context) {
    EvaluationResult result;
    for (const std::string& node_id : snapshot_.topological_order()) {
        const NodeDefinition* node = snapshot_.FindNode(node_id);
        if (node == nullptr) {
            continue;
        }
        if (static_cast<int>(node->frequency) >
            static_cast<int>(context.frequency)) {
            continue;
        }
        bool missing_input = false;
        std::string missing_node;
        std::string missing_port;
        for (const auto& connection : node->inputs) {
            const auto source_it = result.values_by_node.find(
                connection.source_node);
            if (source_it == result.values_by_node.end() ||
                source_it->second.count(connection.source_port) == 0u) {
                missing_input = true;
                missing_node = connection.source_node;
                missing_port = connection.source_port;
                break;
            }
        }
        if (missing_input) {
            result.ok = false;
            result.diagnostics.push_back(EvalDiagnostic(
                *node, "evaluation is missing input value '" + missing_node +
                           "." + missing_port + "' for node '" + node->id +
                           "'"));
            return result;
        }
        std::string key = node_id + "|" + std::string(ToString(node->frequency));
        if (node->frequency == Frequency::Frame) {
            key += "|frame=" + std::to_string(context.frame_index);
            key += "|time=" + NumberBitsHex(context.time_seconds);
        }
        key += "|seed=" + std::to_string(context.seed_base);

        CacheEntry* entry = nullptr;
        std::size_t entry_index = 0;
        auto& entries = entries_[node_id];
        for (std::size_t i = 0; i < entries.size(); ++i) {
            if (entries[i].key == key) {
                entry = &entries[i];
                entry_index = i;
                break;
            }
        }
        if (entry != nullptr && !entry->invalid && !entry->corrupt) {
            ++result.stats.hits;
            result.values_by_node[node_id] = entry->outputs;
            result.cache_hit_by_node[node_id] = true;
            continue;
        }
        if (entry != nullptr) {
            ++result.stats.misses;
            ++result.stats.regenerated;
        } else {
            ++result.stats.misses;
        }

        OutputValues outputs;
        Diagnostic failure;
        if (!EvaluateNode(snapshot_, *node, result.values_by_node, context,
                          outputs, failure)) {
            result.ok = false;
            result.diagnostics.push_back(std::move(failure));
            return result;
        }

        if (entry != nullptr) {
            entries[entry_index].outputs = outputs;
            entries[entry_index].invalid = false;
            entries[entry_index].corrupt = false;
        } else {
            CacheEntry created;
            created.key = key;
            created.outputs = outputs;
            entries.push_back(std::move(created));
        }
        result.values_by_node[node_id] = std::move(outputs);
        result.cache_hit_by_node[node_id] = false;
    }
    result.ok = true;
    return result;
}

CompilerAcceptance AcceptGraphForCompilation(
    const GraphDocument& document,
    const ObservedHardwareMetadata& observed,
    bool gpu_available,
    const OperatorRegistry& registry) {
    (void)gpu_available;
    CompilerAcceptance result;
    ValidationResult validation = ValidateGraphDocument(document, registry);
    result.diagnostics = validation.diagnostics;
    if (!validation.ok()) {
        return result;
    }
    const CapabilityProfileIdentity target = SelectTargetProfile(observed);
    if (validation.snapshot->profile().id != target.id ||
        validation.snapshot->profile().requirements_digest !=
            target.requirements_digest) {
        Diagnostic diagnostic = MakeDiagnostic(
            DiagnosticCode::CapabilityProfileDigestMismatch,
            "graph profile does not match the selected target capability "
            "profile");
        result.diagnostics.push_back(std::move(diagnostic));
        return result;
    }
    result.accepted = true;
    result.profile_id = target.id;
    result.requirements_digest = target.requirements_digest;
    result.semantic_digest = validation.snapshot->semantic_digest();
    return result;
}

AuthoringSession::AuthoringSession(OperatorRegistry registry)
    : registry_(std::move(registry)) {}

ValidationResult AuthoringSession::ApplyEdit(std::string_view text,
                                             std::string source_name) {
    ParseResult parsed = ParseGraphText(text, registry_, std::move(source_name));
    if (!parsed.document || HasErrors(parsed.diagnostics)) {
        last_diagnostics_ = parsed.diagnostics;
        ValidationResult result;
        result.diagnostics = std::move(parsed.diagnostics);
        return result;
    }
    ValidationResult validation =
        ValidateGraphDocument(*parsed.document, registry_);
    if (!validation.ok()) {
        last_diagnostics_ = validation.diagnostics;
        return validation;
    }
    last_valid_ = *validation.snapshot;
    last_layout_ = parsed.document->layout;
    last_diagnostics_ = validation.diagnostics;
    return validation;
}

std::vector<Diagnostic> ValidateCapabilityProfile(
    const CapabilityProfileIdentity& identity) {
    std::vector<Diagnostic> diagnostics;
    ValidateCapabilityIdentity(identity, diagnostics);
    return diagnostics;
}

} // namespace nw::graph
