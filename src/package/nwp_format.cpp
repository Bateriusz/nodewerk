#include "nwp_format.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <functional>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <tuple>

namespace nw::package {

namespace {

// ---------------------------------------------------------------------------
// SHA-256 (portable, small, deterministic)
// ---------------------------------------------------------------------------

class Sha256 {
public:
    Sha256() { Reset(); }

    void Update(const std::uint8_t* data, std::size_t size) {
        for (std::size_t i = 0; i < size; ++i) {
            buffer_[buffer_size_++] = data[i];
            if (buffer_size_ == 64u) {
                Transform(buffer_);
                buffer_size_ = 0u;
            }
        }
        bit_length_ += static_cast<std::uint64_t>(size) * 8u;
    }

    std::array<std::uint8_t, 32> Final() {
        const std::uint64_t bit_length = bit_length_;
        buffer_[buffer_size_++] = 0x80u;
        if (buffer_size_ > 56u) {
            while (buffer_size_ < 64u) {
                buffer_[buffer_size_++] = 0u;
            }
            Transform(buffer_);
            buffer_size_ = 0u;
        }
        while (buffer_size_ < 56u) {
            buffer_[buffer_size_++] = 0u;
        }
        for (int shift = 56; shift >= 0; shift -= 8) {
            buffer_[buffer_size_++] =
                static_cast<std::uint8_t>((bit_length >> shift) & 0xffu);
        }
        Transform(buffer_);
        std::array<std::uint8_t, 32> result{};
        for (std::size_t i = 0; i < 8u; ++i) {
            result[i * 4u + 0u] =
                static_cast<std::uint8_t>((state_[i] >> 24) & 0xffu);
            result[i * 4u + 1u] =
                static_cast<std::uint8_t>((state_[i] >> 16) & 0xffu);
            result[i * 4u + 2u] =
                static_cast<std::uint8_t>((state_[i] >> 8) & 0xffu);
            result[i * 4u + 3u] =
                static_cast<std::uint8_t>(state_[i] & 0xffu);
        }
        return result;
    }

private:
    static std::uint32_t RotateRight(std::uint32_t value,
                                     std::uint32_t count) {
        return (value >> count) | (value << (32u - count));
    }

    void Reset() {
        state_[0] = 0x6a09e667u;
        state_[1] = 0xbb67ae85u;
        state_[2] = 0x3c6ef372u;
        state_[3] = 0xa54ff53au;
        state_[4] = 0x510e527fu;
        state_[5] = 0x9b05688cu;
        state_[6] = 0x1f83d9abu;
        state_[7] = 0x5be0cd19u;
        buffer_size_ = 0u;
        bit_length_ = 0u;
    }

    void Transform(const std::uint8_t* block) {
        static constexpr std::uint32_t k[64] = {
            0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u,
            0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
            0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
            0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
            0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
            0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
            0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u,
            0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
            0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
            0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
            0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u,
            0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
            0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u,
            0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
            0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
            0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u};
        std::uint32_t w[64]{};
        for (std::size_t i = 0; i < 16u; ++i) {
            w[i] = (static_cast<std::uint32_t>(block[i * 4u + 0u]) << 24) |
                   (static_cast<std::uint32_t>(block[i * 4u + 1u]) << 16) |
                   (static_cast<std::uint32_t>(block[i * 4u + 2u]) << 8) |
                   static_cast<std::uint32_t>(block[i * 4u + 3u]);
        }
        for (std::size_t i = 16u; i < 64u; ++i) {
            const std::uint32_t s0 = RotateRight(w[i - 15u], 7u) ^
                                     RotateRight(w[i - 15u], 18u) ^
                                     (w[i - 15u] >> 3u);
            const std::uint32_t s1 = RotateRight(w[i - 2u], 17u) ^
                                     RotateRight(w[i - 2u], 19u) ^
                                     (w[i - 2u] >> 10u);
            w[i] = w[i - 16u] + s0 + w[i - 7u] + s1;
        }
        std::uint32_t a = state_[0];
        std::uint32_t b = state_[1];
        std::uint32_t c = state_[2];
        std::uint32_t d = state_[3];
        std::uint32_t e = state_[4];
        std::uint32_t f = state_[5];
        std::uint32_t g = state_[6];
        std::uint32_t h = state_[7];
        for (std::size_t i = 0; i < 64u; ++i) {
            const std::uint32_t s1 =
                RotateRight(e, 6u) ^ RotateRight(e, 11u) ^ RotateRight(e, 25u);
            const std::uint32_t ch = (e & f) ^ ((~e) & g);
            const std::uint32_t temp1 = h + s1 + ch + k[i] + w[i];
            const std::uint32_t s0 =
                RotateRight(a, 2u) ^ RotateRight(a, 13u) ^ RotateRight(a, 22u);
            const std::uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            const std::uint32_t temp2 = s0 + maj;
            h = g;
            g = f;
            f = e;
            e = d + temp1;
            d = c;
            c = b;
            b = a;
            a = temp1 + temp2;
        }
        state_[0] += a;
        state_[1] += b;
        state_[2] += c;
        state_[3] += d;
        state_[4] += e;
        state_[5] += f;
        state_[6] += g;
        state_[7] += h;
    }

    std::uint32_t state_[8]{};
    std::uint8_t buffer_[64]{};
    std::size_t buffer_size_ = 0u;
    std::uint64_t bit_length_ = 0u;
};

std::string Hex(const std::uint8_t* data, std::size_t size) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string out;
    out.reserve(size * 2u);
    for (std::size_t i = 0; i < size; ++i) {
        out.push_back(kHex[(data[i] >> 4) & 0x0fu]);
        out.push_back(kHex[data[i] & 0x0fu]);
    }
    return out;
}

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

void AddDiagnostic(std::vector<Diagnostic>& diagnostics, std::string code,
                   std::string message, std::string section = {},
                   std::uint64_t offset = 0, bool has_offset = false) {
    Diagnostic diagnostic;
    diagnostic.code = std::move(code);
    diagnostic.message = std::move(message);
    diagnostic.section = std::move(section);
    diagnostic.offset = offset;
    diagnostic.has_offset = has_offset;
    diagnostics.push_back(std::move(diagnostic));
}

bool HasErrors(const std::vector<Diagnostic>& diagnostics) {
    return !diagnostics.empty();
}

std::uint16_t ReadU16(const std::uint8_t* data) {
    return static_cast<std::uint16_t>(data[0]) |
           static_cast<std::uint16_t>(data[1] << 8);
}

std::uint32_t ReadU32(const std::uint8_t* data) {
    return static_cast<std::uint32_t>(data[0]) |
           (static_cast<std::uint32_t>(data[1]) << 8) |
           (static_cast<std::uint32_t>(data[2]) << 16) |
           (static_cast<std::uint32_t>(data[3]) << 24);
}

std::uint64_t ReadU64(const std::uint8_t* data) {
    std::uint64_t value = 0;
    for (int i = 7; i >= 0; --i) {
        value = (value << 8) | data[i];
    }
    return value;
}

class Writer {
public:
    std::vector<std::uint8_t> bytes;
    bool ok = true;
    std::string failure;

    void U8(std::uint8_t value) { bytes.push_back(value); }

    void U16(std::uint16_t value) {
        bytes.push_back(static_cast<std::uint8_t>(value & 0xffu));
        bytes.push_back(static_cast<std::uint8_t>((value >> 8) & 0xffu));
    }

    void U32(std::uint32_t value) {
        bytes.push_back(static_cast<std::uint8_t>(value & 0xffu));
        bytes.push_back(static_cast<std::uint8_t>((value >> 8) & 0xffu));
        bytes.push_back(static_cast<std::uint8_t>((value >> 16) & 0xffu));
        bytes.push_back(static_cast<std::uint8_t>((value >> 24) & 0xffu));
    }

    void U64(std::uint64_t value) {
        for (int i = 0; i < 8; ++i) {
            bytes.push_back(static_cast<std::uint8_t>((value >> (i * 8)) & 0xffu));
        }
    }

    void I64(std::int64_t value) { U64(static_cast<std::uint64_t>(value)); }

    void Bool(bool value) { U8(value ? 1u : 0u); }

    void String(std::string_view value) {
        if (value.size() > kMaxStringBytes) {
            ok = false;
            failure = "string field exceeds the v1 string limit";
            return;
        }
        U16(static_cast<std::uint16_t>(value.size()));
        bytes.insert(bytes.end(), value.begin(), value.end());
    }

    void Blob(const std::vector<std::uint8_t>& value) {
        if (value.size() > kMaxBlobBytes) {
            ok = false;
            failure = "blob field exceeds the v1 blob limit";
            return;
        }
        U32(static_cast<std::uint32_t>(value.size()));
        bytes.insert(bytes.end(), value.begin(), value.end());
    }

    void StringList(const std::vector<std::string>& values) {
        if (values.size() > kMaxRecordCount) {
            ok = false;
            failure = "string list exceeds the v1 record limit";
            return;
        }
        U16(static_cast<std::uint16_t>(values.size()));
        for (const auto& value : values) {
            String(value);
        }
    }
};

struct Reader {
    const std::uint8_t* data = nullptr;
    std::size_t size = 0;
    std::size_t position = 0;
    std::vector<Diagnostic>* diagnostics = nullptr;
    std::string section;
    std::uint64_t base_offset = 0;
    bool failed = false;

    std::size_t Remaining() const { return position <= size ? size - position : 0; }

    bool Take(std::size_t count, const std::uint8_t*& out) {
        if (count > Remaining()) {
            AddDiagnostic(*diagnostics, "package_truncated",
                          "section " + section + " needs " +
                              std::to_string(count) + " bytes but only " +
                              std::to_string(Remaining()) + " remain",
                          section, base_offset + position, true);
            failed = true;
            return false;
        }
        out = data + position;
        position += count;
        return true;
    }

    bool U8(std::uint8_t& value) {
        const std::uint8_t* raw = nullptr;
        if (!Take(1, raw)) return false;
        value = raw[0];
        return true;
    }

    bool U16(std::uint16_t& value) {
        const std::uint8_t* raw = nullptr;
        if (!Take(2, raw)) return false;
        value = ReadU16(raw);
        return true;
    }

    bool U32(std::uint32_t& value) {
        const std::uint8_t* raw = nullptr;
        if (!Take(4, raw)) return false;
        value = ReadU32(raw);
        return true;
    }

    bool U64(std::uint64_t& value) {
        const std::uint8_t* raw = nullptr;
        if (!Take(8, raw)) return false;
        value = ReadU64(raw);
        return true;
    }

    bool I64(std::int64_t& value) {
        std::uint64_t raw = 0;
        if (!U64(raw)) return false;
        value = static_cast<std::int64_t>(raw);
        return true;
    }

    bool Bool(bool& value) {
        std::uint8_t raw = 0;
        if (!U8(raw)) return false;
        if (raw > 1u) {
            AddDiagnostic(*diagnostics, "package_malformed_value",
                          "non-boolean byte in section " + section, section,
                          base_offset + position - 1u, true);
            failed = true;
            return false;
        }
        value = raw != 0u;
        return true;
    }

    bool String(std::string& value) {
        std::uint16_t length = 0;
        if (!U16(length)) return false;
        if (length > kMaxStringBytes) {
            AddDiagnostic(*diagnostics, "package_decode_limit",
                          "string length exceeds limit in section " + section,
                          section, base_offset + position, true);
            failed = true;
            return false;
        }
        const std::uint8_t* raw = nullptr;
        if (!Take(length, raw)) return false;
        value.assign(reinterpret_cast<const char*>(raw), length);
        return true;
    }

    bool Blob(std::vector<std::uint8_t>& value,
              std::uint32_t limit = kMaxBlobBytes) {
        std::uint32_t length = 0;
        if (!U32(length)) return false;
        if (length > limit) {
            AddDiagnostic(*diagnostics, "package_decode_limit",
                          "blob length " + std::to_string(length) +
                              " exceeds limit in section " + section,
                          section, base_offset + position, true);
            failed = true;
            return false;
        }
        const std::uint8_t* raw = nullptr;
        if (!Take(length, raw)) return false;
        value.assign(raw, raw + length);
        return true;
    }

    bool StringList(std::vector<std::string>& values) {
        std::uint16_t count = 0;
        if (!U16(count)) return false;
        if (count > kMaxRecordCount) {
            AddDiagnostic(*diagnostics, "package_decode_limit",
                          "string list count exceeds limit in section " + section,
                          section, base_offset + position, true);
            failed = true;
            return false;
        }
        values.clear();
        for (std::uint16_t i = 0; i < count; ++i) {
            std::string value;
            if (!String(value)) return false;
            values.push_back(std::move(value));
        }
        return true;
    }

    bool AtEnd() const { return Remaining() == 0u; }
};

enum class FieldType : std::uint8_t {
    U32 = 1,
    U64 = 2,
    I64 = 3,
    Bool = 4,
    String = 5,
    StringList = 6,
    Bytes = 7,
};

std::map<std::string, FieldType> CapabilityFields() {
    return {
        {"agility_sdk_required", FieldType::Bool},
        {"compiler_fingerprint", FieldType::String},
        {"direct_queue_count", FieldType::U32},
        {"emitted_shader_targets", FieldType::StringList},
        {"feature_level", FieldType::U32},
        {"limit_max_buffer_bytes", FieldType::U64},
        {"limit_max_compute_threads_per_group", FieldType::U32},
        {"limit_max_texture_2d_dimension", FieldType::U32},
        {"limit_swap_chain_buffer_count", FieldType::U32},
        {"max_cbv_srv_uav_descriptors_per_frame", FieldType::U32},
        {"max_root_signature_dword_cost", FieldType::U32},
        {"max_sampler_descriptors_per_frame", FieldType::U32},
        {"min_windows_build", FieldType::U32},
        {"optional_profile_id", FieldType::String},
        {"optional_profile_separate_artifact", FieldType::Bool},
        {"optional_requirements_digest", FieldType::String},
        {"profile_fingerprint", FieldType::String},
        {"profile_id", FieldType::String},
        {"required_dxgi_formats", FieldType::StringList},
        {"required_frame_instructions", FieldType::StringList},
        {"required_init_instructions", FieldType::StringList},
        {"requirements_digest", FieldType::String},
        {"resource_binding_tier", FieldType::U32},
        {"resource_heap_tier", FieldType::U32},
        {"runtime_dxc_dependency", FieldType::Bool},
        {"runtime_slang_dependency", FieldType::Bool},
        {"shader_model", FieldType::U32},
        {"shader_model_text", FieldType::String},
        {"toolchain_dxc_version", FieldType::String},
        {"toolchain_identity", FieldType::String},
    };
}

std::string CanonicalCapabilityJson(const CapabilityRecord& capability) {
    const auto fields = CapabilityFields();
    std::ostringstream out;
    out << "{";
    bool first = true;
    for (const auto& entry : fields) {
        const std::string& name = entry.first;
        if (name == "profile_fingerprint") {
            continue;
        }
        if (!first) out << ",";
        first = false;
        out << JsonEscape(name) << ":";
        if (name == "agility_sdk_required") {
            out << (capability.agility_sdk_required ? "true" : "false");
        } else if (name == "compiler_fingerprint") {
            out << JsonEscape(capability.compiler_fingerprint);
        } else if (name == "direct_queue_count") {
            out << capability.direct_queue_count;
        } else if (name == "emitted_shader_targets") {
            out << "[";
            for (std::size_t i = 0; i < capability.emitted_shader_targets.size(); ++i) {
                if (i != 0) out << ",";
                out << JsonEscape(capability.emitted_shader_targets[i]);
            }
            out << "]";
        } else if (name == "feature_level") {
            out << capability.feature_level;
        } else if (name == "limit_max_buffer_bytes") {
            out << capability.limit_max_buffer_bytes;
        } else if (name == "limit_max_compute_threads_per_group") {
            out << capability.limit_max_compute_threads_per_group;
        } else if (name == "limit_max_texture_2d_dimension") {
            out << capability.limit_max_texture_2d_dimension;
        } else if (name == "limit_swap_chain_buffer_count") {
            out << capability.limit_swap_chain_buffer_count;
        } else if (name == "max_cbv_srv_uav_descriptors_per_frame") {
            out << capability.max_cbv_srv_uav_descriptors_per_frame;
        } else if (name == "max_root_signature_dword_cost") {
            out << capability.max_root_signature_dword_cost;
        } else if (name == "max_sampler_descriptors_per_frame") {
            out << capability.max_sampler_descriptors_per_frame;
        } else if (name == "min_windows_build") {
            out << capability.min_windows_build;
        } else if (name == "optional_profile_id") {
            out << JsonEscape(capability.optional_profile_id);
        } else if (name == "optional_profile_separate_artifact") {
            out << (capability.optional_profile_separate_artifact ? "true" : "false");
        } else if (name == "optional_requirements_digest") {
            out << JsonEscape(capability.optional_requirements_digest);
        } else if (name == "profile_id") {
            out << JsonEscape(capability.profile_id);
        } else if (name == "required_dxgi_formats") {
            out << "[";
            for (std::size_t i = 0; i < capability.required_dxgi_formats.size(); ++i) {
                if (i != 0) out << ",";
                out << JsonEscape(capability.required_dxgi_formats[i]);
            }
            out << "]";
        } else if (name == "required_frame_instructions") {
            out << "[";
            for (std::size_t i = 0; i < capability.required_frame_instructions.size(); ++i) {
                if (i != 0) out << ",";
                out << JsonEscape(capability.required_frame_instructions[i]);
            }
            out << "]";
        } else if (name == "required_init_instructions") {
            out << "[";
            for (std::size_t i = 0; i < capability.required_init_instructions.size(); ++i) {
                if (i != 0) out << ",";
                out << JsonEscape(capability.required_init_instructions[i]);
            }
            out << "]";
        } else if (name == "requirements_digest") {
            out << JsonEscape(capability.requirements_digest);
        } else if (name == "resource_binding_tier") {
            out << capability.resource_binding_tier;
        } else if (name == "resource_heap_tier") {
            out << capability.resource_heap_tier;
        } else if (name == "runtime_dxc_dependency") {
            out << (capability.runtime_dxc_dependency ? "true" : "false");
        } else if (name == "runtime_slang_dependency") {
            out << (capability.runtime_slang_dependency ? "true" : "false");
        } else if (name == "shader_model") {
            out << capability.shader_model;
        } else if (name == "shader_model_text") {
            out << JsonEscape(capability.shader_model_text);
        } else if (name == "toolchain_dxc_version") {
            out << JsonEscape(capability.toolchain_dxc_version);
        } else if (name == "toolchain_identity") {
            out << JsonEscape(capability.toolchain_identity);
        }
    }
    out << "}";
    return out.str();
}

std::vector<std::uint8_t> EncodeCapability(const CapabilityRecord& capability) {
    Writer writer;
    writer.U16(1);
    writer.U16(0); // flags, must be zero in v1
    const auto fields = CapabilityFields();
    writer.U32(static_cast<std::uint32_t>(fields.size()));
    for (const auto& [name, type] : fields) {
        writer.String(name);
        writer.U8(static_cast<std::uint8_t>(type));
        if (name == "agility_sdk_required") {
            writer.Bool(capability.agility_sdk_required);
        } else if (name == "compiler_fingerprint") {
            writer.String(capability.compiler_fingerprint);
        } else if (name == "direct_queue_count") {
            writer.U32(capability.direct_queue_count);
        } else if (name == "emitted_shader_targets") {
            writer.StringList(capability.emitted_shader_targets);
        } else if (name == "feature_level") {
            writer.U32(capability.feature_level);
        } else if (name == "limit_max_buffer_bytes") {
            writer.U64(capability.limit_max_buffer_bytes);
        } else if (name == "limit_max_compute_threads_per_group") {
            writer.U32(capability.limit_max_compute_threads_per_group);
        } else if (name == "limit_max_texture_2d_dimension") {
            writer.U32(capability.limit_max_texture_2d_dimension);
        } else if (name == "limit_swap_chain_buffer_count") {
            writer.U32(capability.limit_swap_chain_buffer_count);
        } else if (name == "max_cbv_srv_uav_descriptors_per_frame") {
            writer.U32(capability.max_cbv_srv_uav_descriptors_per_frame);
        } else if (name == "max_root_signature_dword_cost") {
            writer.U32(capability.max_root_signature_dword_cost);
        } else if (name == "max_sampler_descriptors_per_frame") {
            writer.U32(capability.max_sampler_descriptors_per_frame);
        } else if (name == "min_windows_build") {
            writer.U32(capability.min_windows_build);
        } else if (name == "optional_profile_id") {
            writer.String(capability.optional_profile_id);
        } else if (name == "optional_profile_separate_artifact") {
            writer.Bool(capability.optional_profile_separate_artifact);
        } else if (name == "optional_requirements_digest") {
            writer.String(capability.optional_requirements_digest);
        } else if (name == "profile_fingerprint") {
            writer.String(capability.profile_fingerprint);
        } else if (name == "profile_id") {
            writer.String(capability.profile_id);
        } else if (name == "required_dxgi_formats") {
            writer.StringList(capability.required_dxgi_formats);
        } else if (name == "required_frame_instructions") {
            writer.StringList(capability.required_frame_instructions);
        } else if (name == "required_init_instructions") {
            writer.StringList(capability.required_init_instructions);
        } else if (name == "requirements_digest") {
            writer.String(capability.requirements_digest);
        } else if (name == "resource_binding_tier") {
            writer.U32(capability.resource_binding_tier);
        } else if (name == "resource_heap_tier") {
            writer.U32(capability.resource_heap_tier);
        } else if (name == "runtime_dxc_dependency") {
            writer.Bool(capability.runtime_dxc_dependency);
        } else if (name == "runtime_slang_dependency") {
            writer.Bool(capability.runtime_slang_dependency);
        } else if (name == "shader_model") {
            writer.U32(capability.shader_model);
        } else if (name == "shader_model_text") {
            writer.String(capability.shader_model_text);
        } else if (name == "toolchain_dxc_version") {
            writer.String(capability.toolchain_dxc_version);
        } else if (name == "toolchain_identity") {
            writer.String(capability.toolchain_identity);
        }
    }
    return writer.ok ? writer.bytes : std::vector<std::uint8_t>{};
}

std::vector<std::uint8_t> EncodeResources(
    const std::vector<ResourceDescriptor>& resources) {
    Writer writer;
    writer.U16(1);
    writer.U32(static_cast<std::uint32_t>(resources.size()));
    for (const auto& resource : resources) {
        writer.U32(resource.resource_id);
        writer.String(resource.name);
        writer.String(resource.kind);
        writer.String(resource.format);
        writer.String(resource.usage);
        writer.String(resource.pass_family);
        writer.U32(resource.binding_slot);
        writer.U32(resource.binding_space);
        writer.U32(resource.width);
        writer.U32(resource.height);
        writer.U32(resource.depth);
        writer.U32(resource.array_size);
        writer.U32(resource.mip_levels);
        writer.U64(resource.size_bytes);
        writer.U32(resource.flags);
    }
    return writer.ok ? writer.bytes : std::vector<std::uint8_t>{};
}

std::vector<std::uint8_t> EncodeInstructions(
    const std::vector<Instruction>& instructions) {
    Writer writer;
    writer.U16(1);
    writer.U32(static_cast<std::uint32_t>(instructions.size()));
    for (const auto& instruction : instructions) {
        writer.String(instruction.opcode);
        writer.U32(static_cast<std::uint32_t>(instruction.operands.size()));
        for (const auto& operand : instruction.operands) {
            writer.U8(static_cast<std::uint8_t>(operand.type));
            switch (operand.type) {
            case OperandType::U32:
            case OperandType::ResourceRef:
            case OperandType::ShaderRef:
            case OperandType::RootSignatureRef:
                writer.U32(operand.u32);
                break;
            case OperandType::U64:
                writer.U64(operand.u64);
                break;
            case OperandType::I64:
                writer.I64(operand.i64);
                break;
            case OperandType::Bool:
                writer.Bool(operand.boolean);
                break;
            case OperandType::String:
                writer.String(operand.string_value);
                break;
            case OperandType::F32: {
                float value = static_cast<float>(operand.f32);
                std::uint32_t bits = 0;
                static_assert(sizeof(value) == sizeof(bits));
                std::memcpy(&bits, &value, sizeof(bits));
                writer.U32(bits);
                break;
            }
            }
        }
    }
    return writer.ok ? writer.bytes : std::vector<std::uint8_t>{};
}

std::vector<std::uint8_t> EncodeParameters(
    const std::vector<ParameterRecord>& parameters) {
    Writer writer;
    writer.U16(1);
    writer.U32(static_cast<std::uint32_t>(parameters.size()));
    for (const auto& parameter : parameters) {
        writer.U32(parameter.parameter_id);
        writer.String(parameter.name);
        writer.String(parameter.type);
        writer.String(parameter.phase);
        writer.U32(parameter.component_count);
        writer.Blob(parameter.value);
        writer.U64(parameter.timeline_start_frame);
        writer.U64(parameter.timeline_end_frame);
        writer.U32(parameter.timeline_step);
        writer.String(parameter.timeline_interpolation);
    }
    return writer.ok ? writer.bytes : std::vector<std::uint8_t>{};
}

template <typename Record, typename GetBlob>
std::vector<std::uint8_t> EncodeBlobTableRecords(
    const std::vector<Record>& records, GetBlob get_blob,
    const std::function<void(Writer&, const Record&, std::uint32_t)>& write_record) {
    Writer writer;
    std::map<std::string, std::uint32_t> indices;
    std::vector<std::vector<std::uint8_t>> blobs;
    std::vector<std::uint32_t> record_indices;
    for (const auto& record : records) {
        const auto& blob = get_blob(record);
        const std::string digest = Sha256Hex(blob);
        auto found = indices.find(digest);
        if (found == indices.end()) {
            const std::uint32_t index = static_cast<std::uint32_t>(blobs.size());
            indices.emplace(digest, index);
            blobs.push_back(blob);
            record_indices.push_back(index);
        } else {
            record_indices.push_back(found->second);
        }
    }
    writer.U16(1);
    writer.U32(static_cast<std::uint32_t>(blobs.size()));
    for (const auto& blob : blobs) {
        writer.Blob(blob);
    }
    writer.U32(static_cast<std::uint32_t>(records.size()));
    for (std::size_t i = 0; i < records.size(); ++i) {
        write_record(writer, records[i], record_indices[i]);
    }
    return writer.ok ? writer.bytes : std::vector<std::uint8_t>{};
}

std::vector<std::uint8_t> EncodeRootSignatures(
    const std::vector<RootSignatureRecord>& records) {
    return EncodeBlobTableRecords<RootSignatureRecord>(
        records, [](const RootSignatureRecord& record) -> const std::vector<std::uint8_t>& {
            return record.blob;
        },
        [](Writer& writer, const RootSignatureRecord& record,
           std::uint32_t blob_index) {
            writer.U32(record.root_signature_id);
            writer.String(record.family);
            writer.String(record.convention);
            writer.U32(record.dword_cost);
            writer.U32(blob_index);
            writer.U32(static_cast<std::uint32_t>(record.bindings.size()));
            for (const auto& binding : record.bindings) {
                writer.String(binding.name);
                writer.String(binding.usage);
                writer.U32(binding.slot);
                writer.U32(binding.space);
                writer.U32(binding.root_parameter);
            }
        });
}

std::vector<std::uint8_t> EncodeShaders(
    const std::vector<ShaderRecord>& records) {
    return EncodeBlobTableRecords<ShaderRecord>(
        records, [](const ShaderRecord& record) -> const std::vector<std::uint8_t>& {
            return record.blob;
        },
        [](Writer& writer, const ShaderRecord& record, std::uint32_t blob_index) {
            writer.U32(record.shader_id);
            writer.String(record.entry_point);
            writer.String(record.target);
            writer.U32(blob_index);
            writer.U32(record.root_signature_id);
            writer.U32(record.flags);
            writer.String(record.family);
            writer.U32(record.thread_group_x);
            writer.U32(record.thread_group_y);
            writer.U32(record.thread_group_z);
        });
}

// ---------------------------------------------------------------------------
// Structural DXIL checks used by the package validator
// ---------------------------------------------------------------------------

bool ValidateDxilContainer(const std::vector<std::uint8_t>& blob,
                           std::string& reason,
                           std::string* measured_model = nullptr) {
    if (blob.size() < 44u) {
        reason = "DXIL container too small";
        return false;
    }
    if (std::memcmp(blob.data(), "DXBC", 4) != 0) {
        reason = "invalid DXIL container magic";
        return false;
    }
    bool nonzero = false;
    for (std::size_t i = 4; i < 20u; ++i) {
        if (blob[i] != 0u) nonzero = true;
    }
    if (!nonzero) {
        reason = "DXIL container has an all-zero unsigned digest";
        return false;
    }
    const std::uint16_t major = ReadU16(blob.data() + 20);
    const std::uint16_t minor = ReadU16(blob.data() + 22);
    const std::uint32_t total_size = ReadU32(blob.data() + 24);
    const std::uint32_t chunk_count = ReadU32(blob.data() + 28);
    if (major != 1u || minor != 0u) {
        reason = "unsupported DXBC container version";
        return false;
    }
    if (total_size != blob.size()) {
        reason = "DXIL container size mismatch";
        return false;
    }
    if (chunk_count < 2u || chunk_count > 64u ||
        32u + chunk_count * 4u > blob.size()) {
        reason = "invalid DXBC chunk count";
        return false;
    }
    bool found_dxil = false;
    bool found_psv0 = false;
    std::string shader_model;
    for (std::uint32_t index = 0; index < chunk_count; ++index) {
        const std::uint32_t offset = ReadU32(blob.data() + 32u + index * 4u);
        // Widen before adding.  `offset + 8u` in 32-bit arithmetic wraps for
        // offset == 0xfffffff8, which would bypass the bounds check and then
        // dereference outside the allocation.
        const std::uint64_t offset64 = offset;
        if (offset < 32u + chunk_count * 4u ||
            offset64 + 8u > blob.size()) {
            reason = "DXBC chunk offset out of bounds";
            return false;
        }
        const std::uint32_t chunk_size = ReadU32(blob.data() + offset + 4u);
        if (static_cast<std::uint64_t>(offset) + 8u + chunk_size >
            blob.size()) {
            reason = "DXBC chunk size exceeds container";
            return false;
        }
        const std::uint8_t* fourcc = blob.data() + offset;
        const std::uint8_t* payload = blob.data() + offset + 8u;
        if (std::memcmp(fourcc, "SFI0", 4) == 0) {
            if (chunk_size != 8u) {
                reason = "invalid SFI0 chunk size";
                return false;
            }
            const std::uint64_t flags = ReadU64(payload);
            const std::uint64_t forbidden = (1ull << 32u) | (1ull << 33u);
            if ((flags & forbidden) != 0u) {
                reason = "SFI0 sets forbidden SM 6.6 heap indexing flags";
                return false;
            }
        } else if (std::memcmp(fourcc, "PSV0", 4) == 0) {
            if (chunk_size < 24u) {
                reason = "PSV0 chunk too small";
                return false;
            }
            found_psv0 = true;
        } else if (std::memcmp(fourcc, "DXIL", 4) == 0) {
            if (chunk_size < 28u) {
                reason = "DXIL chunk too small";
                return false;
            }
            const std::uint32_t program_version = ReadU32(payload);
            const std::uint32_t size_in_u32 = ReadU32(payload + 4);
            const std::uint32_t dxil_version = ReadU32(payload + 12);
            const std::uint32_t bitcode_offset = ReadU32(payload + 16);
            const std::uint32_t bitcode_size = ReadU32(payload + 20);
            const std::uint32_t shader_type = (program_version >> 16u) & 0xffffu;
            const std::uint32_t shader_major = (program_version >> 4u) & 0x0fu;
            const std::uint32_t shader_minor = program_version & 0x0fu;
            std::string shader_type_name;
            if (shader_type == 5u) {
                shader_type_name = "cs";
            } else if (shader_type == 1u) {
                shader_type_name = "vs";
            } else if (shader_type == 0u) {
                shader_type_name = "ps";
            } else {
                shader_type_name = "type" + std::to_string(shader_type);
            }
            shader_model = shader_type_name + "_" +
                           std::to_string(shader_major) + "_" +
                           std::to_string(shader_minor);
            if ((shader_type != 5u && shader_type != 1u && shader_type != 0u) ||
                shader_major != 6u || shader_minor != 0u) {
                reason = "unexpected shader model '" + shader_model + "'";
                return false;
            }
            if (size_in_u32 < 6u ||
                static_cast<std::uint64_t>(size_in_u32) * 4u > chunk_size) {
                reason = "DXIL SizeInUint32 out of bounds";
                return false;
            }
            if (std::memcmp(payload + 8u, "DXIL", 4) != 0) {
                reason = "invalid inner DXIL magic";
                return false;
            }
            const std::uint32_t dxil_major = (dxil_version >> 8u) & 0xffu;
            const std::uint32_t dxil_minor = dxil_version & 0xffu;
            if (dxil_major != 1u || dxil_minor > 8u) {
                reason = "unexpected DXIL version";
                return false;
            }
            if (bitcode_offset < 16u || bitcode_size < 16u) {
                reason = "invalid DXIL bitcode bounds";
                return false;
            }
            const std::uint8_t* bitcode = nullptr;
            const std::uint64_t nested_end =
                8ull + static_cast<std::uint64_t>(bitcode_offset) +
                static_cast<std::uint64_t>(bitcode_size);
            const std::uint64_t direct_end =
                static_cast<std::uint64_t>(bitcode_offset) +
                static_cast<std::uint64_t>(bitcode_size);
            if (nested_end <= chunk_size &&
                std::memcmp(payload + static_cast<std::size_t>(nested_end - bitcode_size),
                            "BC\xc0\xde", 4) == 0) {
                bitcode = payload + static_cast<std::size_t>(
                                       8ull + bitcode_offset);
            } else if (direct_end <= chunk_size &&
                       std::memcmp(payload + static_cast<std::size_t>(bitcode_offset),
                                   "BC\xc0\xde", 4) == 0) {
                bitcode = payload + static_cast<std::size_t>(bitcode_offset);
            } else {
                reason = "DXIL payload does not start with LLVM bitcode magic";
                return false;
            }
            const std::uint32_t first_word = ReadU32(bitcode + 4u);
            const std::uint32_t abbrev_id = first_word & 0x3u;
            const std::uint32_t block_id = (first_word >> 2u) & 0xffu;
            if (abbrev_id != 1u ||
                (block_id != 0u && block_id != 8u && block_id != 13u)) {
                reason = "DXIL payload lacks an LLVM 3.7 MODULE_BLOCK header";
                return false;
            }
            found_dxil = true;
        }
    }
    if (!found_dxil) {
        reason = "DXBC container is missing required DXIL chunk";
        return false;
    }
    if (!found_psv0) {
        reason = "DXBC container is missing required PSV0 chunk";
        return false;
    }
    if (measured_model != nullptr) *measured_model = shader_model;
    return true;
}

// Release packages must not carry shader debug/reflection chunks even when
// their payload does not contain any of the ASCII strings the leak scan looks
// for.  ILDB/ILDN carry debug metadata, STAT carries shader statistics, and
// RDEF/RDAT/SPDB/SPDN carry reflection or symbol data.  The execution-required
// chunks (DXIL, PSV0, SFI0, HASH, input/output signatures) remain untouched.
bool HasForbiddenDxilMetadataChunks(const std::vector<std::uint8_t>& blob,
                                    std::string& reason) {
    if (blob.size() < 44u || std::memcmp(blob.data(), "DXBC", 4) != 0) {
        return false;
    }
    const std::uint32_t chunk_count = ReadU32(blob.data() + 28);
    if (chunk_count == 0u || chunk_count > 64u ||
        32u + static_cast<std::uint64_t>(chunk_count) * 4u > blob.size()) {
        return false;
    }
    static constexpr std::string_view kForbidden[] = {
        "ILDB", "ILDN", "STAT", "RDEF", "RDAT", "SPDB", "SPDN"};
    for (std::uint32_t index = 0; index < chunk_count; ++index) {
        const std::uint32_t offset =
            ReadU32(blob.data() + 32u + index * 4u);
        const std::uint64_t offset64 = offset;
        if (offset < 32u + static_cast<std::uint64_t>(chunk_count) * 4u ||
            offset64 + 8u > blob.size()) {
            continue;
        }
        const std::uint8_t* fourcc = blob.data() + offset;
        for (const auto candidate : kForbidden) {
            if (std::memcmp(fourcc, candidate.data(), 4u) == 0) {
                reason = "shader payload contains forbidden " +
                         std::string(candidate) +
                         " debug/reflection chunk";
                return true;
            }
        }
    }
    return false;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

bool ValidateCapabilityIdentity(const CapabilityRecord& capability,
                                std::vector<Diagnostic>& diagnostics) {
    const std::size_t before = diagnostics.size();
    auto error = [&](std::string code, std::string message) {
        AddDiagnostic(diagnostics, std::move(code), std::move(message),
                      std::string(kSectionCapability));
    };
    const bool common_primary =
        capability.profile_id == kCommonProfileId &&
        capability.requirements_digest == kCommonRequirementsDigest;
    const bool optional_primary =
        capability.profile_id == kOptionalWaveOpsProfileId &&
        capability.requirements_digest ==
            kOptionalWaveOpsRequirementsDigest;
    if (!common_primary && !optional_primary) {
        if (capability.profile_id != kCommonProfileId &&
            capability.profile_id != kOptionalWaveOpsProfileId) {
            error("package_profile_unknown",
                  "unknown capability-profile identity '" +
                      capability.profile_id + "'");
        } else if (capability.profile_id == kCommonProfileId) {
            error("package_profile_digest_mismatch",
                  "capability profile requires requirements digest '" +
                      std::string(kCommonRequirementsDigest) + "', got '" +
                      capability.requirements_digest + "'");
        } else {
            error("package_profile_digest_mismatch",
                  "optional capability profile requires requirements digest '" +
                      std::string(kOptionalWaveOpsRequirementsDigest) +
                      "', got '" + capability.requirements_digest + "'");
        }
    }
    if (!capability.optional_profile_id.empty()) {
        if (capability.optional_profile_id != kOptionalWaveOpsProfileId) {
            error("package_optional_profile_unknown",
                  "unknown optional capability-profile identity '" +
                      capability.optional_profile_id + "'");
        } else if (capability.optional_requirements_digest !=
                   kOptionalWaveOpsRequirementsDigest) {
            error("package_optional_profile_digest_mismatch",
                  "optional profile requirements digest mismatch");
        }
        if (!capability.optional_profile_separate_artifact) {
            error("package_optional_profile_contradiction",
                  "unsupported optional profiles must remain separate artifacts");
        }
    } else if (!capability.optional_requirements_digest.empty()) {
        error("package_optional_profile_contradiction",
              "optional requirements digest is present without an optional profile id");
    }
    return diagnostics.size() == before;
}

bool ValidateCapabilityStatic(const CapabilityRecord& capability,
                              std::vector<Diagnostic>& diagnostics,
                              bool release = true) {
    const std::size_t before = diagnostics.size();
    auto error = [&](std::string code, std::string message) {
        AddDiagnostic(diagnostics, std::move(code), std::move(message),
                      std::string(kSectionCapability));
    };
    ValidateCapabilityIdentity(capability, diagnostics);
    const auto expect = [&](std::string_view name, auto actual, auto expected) {
        if (actual != expected) {
            error("package_capability_contradictory",
                  "CAP field '" + std::string(name) + "' does not match the baseline");
        }
    };
    expect("feature_level", capability.feature_level, kFeatureLevel12_0);
    expect("shader_model", capability.shader_model, kShaderModel6_0);
    expect("resource_binding_tier", capability.resource_binding_tier,
           kResourceBindingTier2);
    expect("resource_heap_tier", capability.resource_heap_tier,
           kResourceHeapTier2);
    expect("direct_queue_count", capability.direct_queue_count, 1u);
    expect("min_windows_build", capability.min_windows_build, kMinWindowsBuild);
    if (capability.shader_model_text != kShaderModelText6_0) {
        error("package_capability_contradictory",
              "shader_model_text does not match shader model 6.0");
    }
    std::set<std::string> unique_formats(capability.required_dxgi_formats.begin(),
                                         capability.required_dxgi_formats.end());
    if (unique_formats.size() != capability.required_dxgi_formats.size()) {
        error("package_capability_contradictory",
              "required DXGI formats contain duplicates");
    }
    for (const auto& format : capability.required_dxgi_formats) {
        if (format != "DXGI_FORMAT_R8G8B8A8_UNORM" &&
            format != "DXGI_FORMAT_R32G32B32A32_FLOAT" &&
            format != "DXGI_FORMAT_R32_UINT" &&
            format != "DXGI_FORMAT_D32_FLOAT") {
            error("package_capability_format_set",
                  "required DXGI format '" + format + "' is unknown");
        }
    }
    if (capability.max_cbv_srv_uav_descriptors_per_frame >
            kMaxCbvSrvUavDescriptorsPerFrame ||
        capability.max_sampler_descriptors_per_frame >
            kMaxSamplerDescriptorsPerFrame ||
        capability.max_root_signature_dword_cost > kMaxRootSignatureDwordCost ||
        capability.limit_max_texture_2d_dimension > kMaxTexture2dDimension ||
        capability.limit_max_compute_threads_per_group >
            kMaxComputeThreadsPerGroup ||
        capability.limit_max_buffer_bytes > kMaxBufferBytes ||
        capability.limit_swap_chain_buffer_count > kSwapChainBufferCount) {
        error("package_capability_contradictory",
              "emitted requirement exceeds the baseline profile limit");
    }
    if (capability.emitted_shader_targets.empty()) {
        error("package_capability_contradictory",
              "emitted_shader_targets is empty");
    }
    for (const auto& target : capability.emitted_shader_targets) {
        if (target != "cs_6_0" && target != "vs_6_0" && target != "ps_6_0") {
            error("package_shader_target_unknown",
                  "unknown emitted shader target '" + target + "'");
        }
    }
    if (capability.runtime_dxc_dependency) {
        error("package_runtime_dependency_forbidden",
              "runtime DXC dependency is forbidden");
    }
    if (capability.runtime_slang_dependency) {
        error("package_runtime_dependency_forbidden",
              "runtime Slang dependency is forbidden");
    }
    if (capability.agility_sdk_required) {
        error("package_capability_contradictory",
              "Agility SDK redistribution is not permitted");
    }
    if (capability.toolchain_dxc_version.empty()) {
        error("package_capability_contradictory", "toolchain_dxc_version is empty");
    }
    if (capability.toolchain_identity.empty()) {
        error("package_capability_contradictory", "toolchain_identity is empty");
    }
    if (release) {
        if (capability.toolchain_dxc_version.find(
                std::string(kPinnedDxcVersion)) ==
            std::string::npos) {
            error("package_toolchain_identity",
                  "release packages require pinned DXC version '" +
                      std::string(kPinnedDxcVersion) + "'");
        }
        const std::string expected_identity =
            "dxc:" + capability.toolchain_dxc_version;
        if (capability.toolchain_identity != expected_identity) {
            error("package_toolchain_identity",
                  "toolchain_identity must match the declared DXC version");
        }
    }
    if (capability.compiler_fingerprint.empty()) {
        error("package_capability_contradictory", "compiler_fingerprint is empty");
    }
    if (capability.profile_fingerprint.empty()) {
        error("package_capability_contradictory",
              "profile_fingerprint is empty");
    } else if (capability.profile_fingerprint !=
               ComputeProfileFingerprint(capability)) {
        error("package_profile_fingerprint_mismatch",
              "profile_fingerprint does not match the capability record");
    }
    for (const auto& values :
         {capability.required_init_instructions,
          capability.required_frame_instructions}) {
        std::set<std::string> unique(values.begin(), values.end());
        if (unique.size() != values.size()) {
            error("package_capability_contradictory",
                  "required instruction list contains duplicates");
        }
    }
    return diagnostics.size() == before;
}

bool ValidateCapability(const CapabilityRecord& capability,
                        const std::vector<ResourceDescriptor>& resources,
                        const std::vector<Instruction>& initialization,
                        const std::vector<Instruction>& frame,
                        const std::vector<ShaderRecord>& shaders,
                        const std::vector<RootSignatureRecord>& root_signatures,
                        std::vector<Diagnostic>& diagnostics,
                        bool release = true) {
    const std::size_t before = diagnostics.size();
    auto error = [&](std::string code, std::string message) {
        AddDiagnostic(diagnostics, std::move(code), std::move(message),
                      std::string(kSectionCapability));
    };
    ValidateCapabilityStatic(capability, diagnostics, release);
    const auto expect = [&](std::string_view name, auto actual, auto expected) {
        if (actual != expected) {
            error("package_capability_contradictory",
                  "CAP field '" + std::string(name) +
                      "' does not match emitted requirements");
        }
    };
    std::set<std::string> emitted_formats;
    for (const auto& resource : resources) {
        emitted_formats.insert(resource.format);
    }
    const std::vector<std::string> expected_formats(emitted_formats.begin(),
                                                    emitted_formats.end());
    if (capability.required_dxgi_formats != expected_formats) {
        error("package_capability_format_set",
              "required DXGI formats must exactly match emitted resources");
    }
    std::set<std::uint32_t> root_constant_resources;
    bool has_present = false;
    for (const auto& instruction : frame) {
        if ((instruction.opcode == "frame.set_root_constants" ||
             instruction.opcode == "frame.set_graphics_root_constants") &&
            instruction.operands.size() == 3u &&
            instruction.operands[1].type == OperandType::ResourceRef) {
            root_constant_resources.insert(instruction.operands[1].u32);
        }
        if (instruction.opcode == "frame.present") has_present = true;
    }
    for (const auto& instruction : initialization) {
        if (instruction.opcode == "init.set_root_constants" &&
            instruction.operands.size() == 3u &&
            instruction.operands[1].type == OperandType::ResourceRef) {
            root_constant_resources.insert(instruction.operands[1].u32);
        }
    }
    std::uint32_t descriptor_count = 0u;
    std::uint32_t sampler_count = 0u;
    std::uint64_t max_buffer_bytes = 0u;
    std::uint32_t max_texture2d_dimension = 0u;
    for (const auto& resource : resources) {
        if (resource.usage == "sampler") {
            ++sampler_count;
        } else if (resource.usage == "uav_srv") {
            descriptor_count += 2u;
        } else if (resource.usage == "rtv_srv") {
            ++descriptor_count;
        } else if (resource.usage == "srv" || resource.usage == "uav" ||
                   (resource.usage == "cbv" &&
                    root_constant_resources.count(resource.resource_id) == 0u)) {
            ++descriptor_count;
        }
        if (resource.kind == "buffer" &&
            resource.size_bytes > max_buffer_bytes) {
            max_buffer_bytes = resource.size_bytes;
        }
        if (resource.kind == "texture2d") {
            max_texture2d_dimension =
                std::max(max_texture2d_dimension,
                         std::max(resource.width, resource.height));
        }
    }
    std::uint32_t max_root_dwords = 0u;
    for (const auto& root : root_signatures) {
        max_root_dwords = std::max(max_root_dwords, root.dword_cost);
    }
    std::uint32_t max_threads = 0u;
    for (const auto& shader : shaders) {
        const std::uint64_t total =
            static_cast<std::uint64_t>(shader.thread_group_x) *
            shader.thread_group_y * shader.thread_group_z;
        if (total > max_threads &&
            total <= std::numeric_limits<std::uint32_t>::max()) {
            max_threads = static_cast<std::uint32_t>(total);
        }
    }
    expect("max_cbv_srv_uav_descriptors_per_frame",
           capability.max_cbv_srv_uav_descriptors_per_frame, descriptor_count);
    expect("max_sampler_descriptors_per_frame",
           capability.max_sampler_descriptors_per_frame, sampler_count);
    expect("max_root_signature_dword_cost",
           capability.max_root_signature_dword_cost, max_root_dwords);
    expect("limit_max_texture_2d_dimension",
           capability.limit_max_texture_2d_dimension, max_texture2d_dimension);
    expect("limit_max_compute_threads_per_group",
           capability.limit_max_compute_threads_per_group, max_threads);
    expect("limit_max_buffer_bytes", capability.limit_max_buffer_bytes,
           max_buffer_bytes);
    expect("limit_swap_chain_buffer_count",
           capability.limit_swap_chain_buffer_count,
           has_present ? kSwapChainBufferCount : 0u);
    (void)initialization;
    return diagnostics.size() == before;
}

bool IsKnownPassFamily(std::string_view family);

bool ValidateResources(const std::vector<ResourceDescriptor>& resources,
                       std::vector<Diagnostic>& diagnostics) {
    const std::size_t before = diagnostics.size();
    std::set<std::uint32_t> ids;
    std::set<std::string> bindings;
    bool has_params = false;
    bool has_output = false;
    for (const auto& resource : resources) {
        auto error = [&](std::string code, std::string message) {
            AddDiagnostic(diagnostics, std::move(code), std::move(message),
                          std::string(kSectionResources));
        };
        if (resource.resource_id == 0u) {
            error("package_resource_id", "resource id must be positive");
        }
        if (!ids.insert(resource.resource_id).second) {
            error("package_resource_id", "duplicate resource id");
        }
        if (resource.name.empty() || resource.pass_family.empty()) {
            error("package_resource_malformed",
                  "resource name and pass_family must be non-empty");
        }
        if (resource.kind != "buffer" && resource.kind != "texture2d" &&
            resource.kind != "texture3d") {
            error("package_resource_kind", "unknown resource kind");
        }
        if (resource.format != "DXGI_FORMAT_R8G8B8A8_UNORM" &&
            resource.format != "DXGI_FORMAT_R32G32B32A32_FLOAT" &&
            resource.format != "DXGI_FORMAT_R32_UINT" &&
            resource.format != "DXGI_FORMAT_D32_FLOAT") {
            error("package_resource_format", "unknown DXGI format");
        }
        if (resource.usage != "uav" && resource.usage != "srv" &&
            resource.usage != "cbv" && resource.usage != "sampler" &&
            resource.usage != "rtv" && resource.usage != "dsv" &&
            resource.usage != "readback" && resource.usage != "uav_srv" &&
            resource.usage != "rtv_srv") {
            error("package_resource_usage", "unknown resource usage");
        }
        const bool has_format =
            resource.format == "DXGI_FORMAT_R8G8B8A8_UNORM" ||
            resource.format == "DXGI_FORMAT_R32G32B32A32_FLOAT" ||
            resource.format == "DXGI_FORMAT_R32_UINT" ||
            resource.format == "DXGI_FORMAT_D32_FLOAT";
        if (resource.kind == "buffer" && !has_format) {
            error("package_resource_format", "buffer resource has no valid format");
        }
        if (resource.kind == "buffer") {
            if (resource.usage != "cbv" && resource.usage != "srv" &&
                resource.usage != "uav" && resource.usage != "uav_srv" &&
                resource.usage != "readback" &&
                resource.usage != "sampler") {
                error("package_resource_usage",
                      "buffer resource has an incompatible usage");
            }
            if (resource.width != 0u || resource.height != 0u ||
                resource.depth != 0u) {
                error("package_resource_dimensions",
                      "buffer resource must not declare texture dimensions");
            }
            if (resource.size_bytes == 0u ||
                resource.size_bytes > kMaxBufferBytes) {
                error("package_resource_dimensions",
                      "buffer size is outside the baseline limits");
            }
            if (resource.array_size != 1u || resource.mip_levels != 1u) {
                error("package_resource_dimensions",
                      "buffer must be a single array slice/mip");
            }
        } else if (resource.kind == "texture2d") {
            if (resource.usage != "srv" && resource.usage != "uav" &&
                resource.usage != "uav_srv" && resource.usage != "rtv" &&
                resource.usage != "rtv_srv" && resource.usage != "dsv") {
                error("package_resource_usage",
                      "texture2d resource has an incompatible usage");
            }
            const bool dimensions_valid =
                resource.width >= 1u &&
                resource.width <= kMaxTexture2dDimension &&
                resource.height >= 1u &&
                resource.height <= kMaxTexture2dDimension &&
                resource.depth <= 1u;
            if (resource.width == 0u ||
                resource.width > kMaxTexture2dDimension ||
                resource.height == 0u ||
                resource.height > kMaxTexture2dDimension) {
                error("package_resource_dimensions",
                      "texture2d dimensions exceed the baseline limit");
            }
            if (resource.depth > 1u) {
                error("package_resource_dimensions",
                      "texture2d depth must be 0 or 1");
            }
            const bool array_valid =
                resource.array_size >= 1u &&
                resource.array_size <= kMaxTextureArraySize;
            if (!array_valid) {
                error("package_resource_dimensions",
                      "texture2d array_size exceeds the baseline limit");
            }
            std::uint32_t max_mip_levels = 0u;
            const std::uint32_t max_dimension =
                std::max(resource.width, resource.height);
            for (std::uint32_t value = max_dimension; value > 0u;
                 value >>= 1u) {
                ++max_mip_levels;
            }
            const bool mip_valid =
                resource.mip_levels >= 1u &&
                resource.mip_levels <= kMaxMipLevels &&
                resource.mip_levels <= max_mip_levels;
            if (!mip_valid) {
                error("package_resource_dimensions",
                      "texture2d mip_levels is invalid for its dimensions");
            }
            const std::uint64_t bytes_per_element =
                resource.format == "DXGI_FORMAT_R32G32B32A32_FLOAT" ? 16u :
                resource.format == "DXGI_FORMAT_R8G8B8A8_UNORM" ? 4u :
                resource.format == "DXGI_FORMAT_D32_FLOAT" ? 4u : 4u;
            std::uint64_t computed = 0u;
            bool overflow = false;
            if (dimensions_valid && array_valid && mip_valid) {
                for (std::uint32_t mip = 0u; mip < resource.mip_levels;
                     ++mip) {
                    const std::uint64_t level_width =
                        std::max(1u, resource.width >> mip);
                    const std::uint64_t level_height =
                        std::max(1u, resource.height >> mip);
                    const std::uint64_t level = level_width * level_height;
                    if (level > UINT64_MAX / bytes_per_element ||
                        level * bytes_per_element >
                            UINT64_MAX / resource.array_size) {
                        overflow = true;
                        break;
                    }
                    computed += level * bytes_per_element *
                                resource.array_size;
                }
            }
            if (dimensions_valid && array_valid && mip_valid &&
                (overflow || computed == 0u ||
                 computed > kMaxBufferBytes ||
                 resource.size_bytes != computed)) {
                error("package_resource_dimensions",
                      "texture2d size does not match its checked allocation");
            }
        } else {
            if (resource.usage != "srv" && resource.usage != "uav") {
                error("package_resource_usage",
                      "texture3d resource has an incompatible usage");
            }
            const bool dimensions_valid =
                resource.width >= 1u && resource.width <= 2048u &&
                resource.height >= 1u && resource.height <= 2048u &&
                resource.depth >= 1u && resource.depth <= 2048u;
            if (!dimensions_valid) {
                error("package_resource_dimensions",
                      "texture3d dimensions exceed 2048");
            }
            const bool array_valid = resource.array_size == 1u;
            if (!array_valid) {
                error("package_resource_dimensions",
                      "texture3d must be a single array slice");
            }
            std::uint32_t max_mip_levels = 0u;
            const std::uint32_t max_dimension =
                std::max(std::max(resource.width, resource.height),
                         resource.depth);
            for (std::uint32_t value = max_dimension; value > 0u;
                 value >>= 1u) {
                ++max_mip_levels;
            }
            const bool mip_valid =
                resource.mip_levels >= 1u &&
                resource.mip_levels <= kMaxMipLevels &&
                resource.mip_levels <= max_mip_levels;
            if (!mip_valid) {
                error("package_resource_dimensions",
                      "texture3d mip_levels is invalid for its dimensions");
            }
            const std::uint64_t bytes_per_element =
                resource.format == "DXGI_FORMAT_R32G32B32A32_FLOAT" ? 16u :
                resource.format == "DXGI_FORMAT_R8G8B8A8_UNORM" ? 4u :
                resource.format == "DXGI_FORMAT_D32_FLOAT" ? 4u : 4u;
            std::uint64_t computed = 0u;
            bool overflow = false;
            if (dimensions_valid && array_valid && mip_valid) {
                for (std::uint32_t mip = 0u; mip < resource.mip_levels;
                     ++mip) {
                    const std::uint64_t level_width =
                        std::max(1u, resource.width >> mip);
                    const std::uint64_t level_height =
                        std::max(1u, resource.height >> mip);
                    const std::uint64_t level_depth =
                        std::max(1u, resource.depth >> mip);
                    const std::uint64_t level =
                        level_width * level_height;
                    if (level_depth != 0u &&
                        level > UINT64_MAX / level_depth) {
                        overflow = true;
                        break;
                    }
                    const std::uint64_t volume = level * level_depth;
                    if (volume > UINT64_MAX / bytes_per_element) {
                        overflow = true;
                        break;
                    }
                    computed += volume * bytes_per_element;
                }
            }
            if (dimensions_valid && array_valid && mip_valid &&
                (overflow || computed == 0u ||
                 computed > kMaxBufferBytes ||
                 resource.size_bytes != computed)) {
                error("package_resource_dimensions",
                      "texture3d size does not match its checked allocation");
            }
        }
        const std::string binding_key = resource.pass_family + "/" +
                                        resource.usage + "/" +
                                        std::to_string(resource.binding_space) +
                                        ":" +
                                        std::to_string(resource.binding_slot);
        if (resource.binding_slot > 4095u || resource.binding_space > 15u) {
            error("package_resource_binding",
                  "resource binding slot/space is out of range");
        }
        const bool transient_shared_range =
            resource.usage == "uav" || resource.usage == "srv";
        if (!bindings.insert(binding_key).second &&
            !transient_shared_range) {
            error("package_resource_binding_collision",
                  "duplicate resource binding");
        }
        if (resource.pass_family == "compute.math_noise") {
            const bool params = resource.usage == "cbv" &&
                                resource.binding_slot == 0u &&
                                resource.binding_space == 0u;
            const bool output = resource.usage == "uav" &&
                                resource.binding_slot == 0u &&
                                resource.binding_space == 0u;
            const bool input = resource.usage == "srv" &&
                               resource.binding_slot == 0u &&
                               resource.binding_space == 0u;
            const bool sampler = resource.usage == "sampler" &&
                                 resource.binding_slot == 0u &&
                                 resource.binding_space == 0u;
            if (!params && !output && !input && !sampler) {
                error("package_resource_binding_contract",
                      "resource does not obey the fixed compute.math_noise convention");
            }
            has_params = has_params || params;
            has_output = has_output || output;
        } else if (!IsKnownPassFamily(resource.pass_family)) {
            error("package_pass_family_unknown",
                  "resource uses an unknown pass family");
        }
    }
    // Family contract applies when a compute.math_noise resource is present.
    const bool family_present = std::any_of(
        resources.begin(), resources.end(), [](const ResourceDescriptor& item) {
            return item.pass_family == "compute.math_noise";
        });
    if (family_present && (!has_params || !has_output)) {
        AddDiagnostic(diagnostics, "package_resource_binding_contract",
                      "compute.math_noise is missing its fixed cbv/uav bindings",
                      std::string(kSectionResources));
    }
    return diagnostics.size() == before;
}

void ValidateTransientBindingReuse(
    const std::vector<ResourceDescriptor>& resources,
    const std::vector<Instruction>& frame,
    std::vector<Diagnostic>& diagnostics) {
    std::map<std::string, std::set<std::uint32_t>> ids_by_binding;
    std::map<std::string, std::string> usage_by_binding;
    std::map<std::uint32_t, std::string> names_by_id;
    for (const auto& resource : resources) {
        const std::string key =
            resource.pass_family + "/" + resource.usage + "/" +
            std::to_string(resource.binding_space) + ":" +
            std::to_string(resource.binding_slot);
        ids_by_binding[key].insert(resource.resource_id);
        usage_by_binding[key] = resource.usage;
        names_by_id[resource.resource_id] = resource.name;
    }
    std::map<std::string, std::set<std::uint32_t>> duplicates;
    for (const auto& entry : ids_by_binding) {
        if (entry.second.size() > 1u) duplicates.insert(entry);
    }
    if (duplicates.empty()) return;
    std::set<std::uint32_t> explicitly_bound;
    for (const auto& instruction : frame) {
        if (instruction.opcode != "frame.set_descriptor_table") continue;
        std::vector<std::uint32_t> bound;
        for (const auto& operand : instruction.operands) {
            if (operand.type == OperandType::ResourceRef) {
                bound.push_back(operand.u32);
            }
        }
        for (const auto& entry : duplicates) {
            std::uint32_t simultaneous = 0u;
            for (const auto resource_id : bound) {
                if (entry.second.count(resource_id) != 0u) {
                    ++simultaneous;
                }
            }
            if (simultaneous > 1u) {
                AddDiagnostic(
                    diagnostics, "package_resource_binding_collision",
                    "descriptor table binds multiple resources competing for " +
                        entry.first,
                    std::string(kSectionFrame));
            }
        }
        explicitly_bound.insert(bound.begin(), bound.end());
    }
    for (const auto& entry : duplicates) {
        const auto usage_it = usage_by_binding.find(entry.first);
        if (usage_it == usage_by_binding.end() ||
            usage_it->second != "uav") {
            continue;
        }
        for (const auto resource_id : entry.second) {
            if (explicitly_bound.count(resource_id) != 0u) continue;
            const auto name_it = names_by_id.find(resource_id);
            AddDiagnostic(
                diagnostics, "package_resource_binding_collision",
                "resource '" +
                    (name_it == names_by_id.end() ? std::to_string(resource_id)
                                                  : name_it->second) +
                    "' shares a transient binding but is never explicitly bound",
                std::string(kSectionResources));
        }
    }
}

struct OperandContract {
    const char* opcode;
    const char* phase;
    std::vector<OperandType> operands;
};

std::vector<OperandContract> InstructionContracts() {
    return {
        {"init.create_resource", "initialization", {OperandType::ResourceRef}},
        {"init.create_root_signature", "initialization",
         {OperandType::RootSignatureRef}},
        {"init.create_pipeline", "initialization",
         {OperandType::ShaderRef, OperandType::RootSignatureRef}},
        {"init.create_graphics_pipeline", "initialization",
         {OperandType::ShaderRef, OperandType::ShaderRef,
          OperandType::RootSignatureRef}},
        {"init.upload_resource", "initialization", {OperandType::ResourceRef}},
        {"init.set_root_signature", "initialization",
         {OperandType::RootSignatureRef}},
        {"init.set_root_constants", "initialization",
         {OperandType::U32, OperandType::ResourceRef, OperandType::U32}},
        {"init.set_descriptor_table", "initialization",
         {OperandType::U32, OperandType::String, OperandType::ResourceRef}},
        {"init.dispatch", "initialization",
         {OperandType::U32, OperandType::U32, OperandType::U32,
          OperandType::ShaderRef}},
        {"frame.set_root_signature", "frame", {OperandType::RootSignatureRef}},
        {"frame.set_graphics_root_signature", "frame",
         {OperandType::RootSignatureRef}},
        {"frame.set_root_constants", "frame",
         {OperandType::U32, OperandType::ResourceRef, OperandType::U32}},
        {"frame.set_descriptor_table", "frame",
         {OperandType::U32, OperandType::String, OperandType::ResourceRef}},
        {"frame.set_graphics_root_constants", "frame",
         {OperandType::U32, OperandType::ResourceRef, OperandType::U32}},
        {"frame.set_graphics_descriptor_table", "frame",
         {OperandType::U32, OperandType::String, OperandType::ResourceRef}},
        {"frame.set_graphics_pipeline", "frame",
         {OperandType::ShaderRef}},
        {"frame.set_render_targets", "frame",
         {OperandType::ResourceRef, OperandType::ResourceRef}},
        {"frame.execute_indirect", "frame",
         {OperandType::ResourceRef, OperandType::U32}},
        {"frame.barrier", "frame",
         {OperandType::U32, OperandType::ResourceRef}},
        {"frame.dispatch", "frame",
         {OperandType::U32, OperandType::U32, OperandType::U32,
          OperandType::ShaderRef}},
        {"frame.draw", "frame", {OperandType::U32, OperandType::U32}},
        {"frame.copy_buffer", "frame",
         {OperandType::ResourceRef, OperandType::ResourceRef, OperandType::U64}},
        {"frame.set_timeline", "frame",
         {OperandType::U32, OperandType::U64}},
        {"frame.present", "frame", {OperandType::ResourceRef}},
    };
}

bool ValidateInstructions(
    const std::vector<Instruction>& instructions,
    std::string_view phase,
    const std::vector<ResourceDescriptor>& resources,
    const std::vector<RootSignatureRecord>& root_signatures,
    const std::vector<ShaderRecord>& shaders,
    const std::map<std::uint32_t, std::uint32_t>& pipeline_roots,
    const std::set<std::uint32_t>& resource_ids,
    const std::set<std::uint32_t>& shader_ids,
    const std::set<std::uint32_t>& root_ids,
    const std::set<std::uint32_t>& parameter_ids,
    std::set<std::string>& actual_opcodes,
    std::vector<Diagnostic>& diagnostics) {
    const std::size_t before = diagnostics.size();
    const std::string section =
        phase == "initialization" ? std::string(kSectionInitialization)
                                  : std::string(kSectionFrame);
    const auto contracts = InstructionContracts();
    std::map<std::uint32_t, const RootSignatureRecord*> root_by_id;
    for (const auto& root : root_signatures) {
        root_by_id[root.root_signature_id] = &root;
    }
    std::map<std::uint32_t, std::uint32_t> shader_root_ids;
    std::map<std::uint32_t, std::string> shader_targets;
    for (const auto& shader : shaders) {
        shader_root_ids[shader.shader_id] = shader.root_signature_id;
        shader_targets[shader.shader_id] = shader.target;
    }
    bool has_active_root_signature = false;
    std::uint32_t active_root_signature_id = 0u;
    for (std::size_t index = 0; index < instructions.size(); ++index) {
        const auto& instruction = instructions[index];
        const OperandContract* contract = nullptr;
        for (const auto& candidate : contracts) {
            if (instruction.opcode == candidate.opcode) {
                contract = &candidate;
                break;
            }
        }
        auto error = [&](std::string code, std::string message) {
            AddDiagnostic(diagnostics, std::move(code), std::move(message),
                          section, index, true);
        };
        if (contract == nullptr) {
            error("package_instruction_unknown",
                  "instruction '" + instruction.opcode + "' is not supported");
            continue;
        }
        actual_opcodes.insert(instruction.opcode);
        if (phase != contract->phase) {
            error("package_instruction_phase",
                  "instruction '" + instruction.opcode + "' belongs in " +
                      contract->phase);
        }
        if (instruction.operands.size() != contract->operands.size()) {
            error("package_instruction_operand_count",
                  "instruction '" + instruction.opcode + "' has the wrong operand count");
            continue;
        }
        if (instruction.opcode == "init.create_pipeline" &&
            instruction.operands[0].type == OperandType::ShaderRef &&
            instruction.operands[1].type == OperandType::RootSignatureRef) {
            const std::uint32_t shader_id = instruction.operands[0].u32;
            const std::uint32_t root_id = instruction.operands[1].u32;
            auto shader_root = shader_root_ids.find(shader_id);
            if (shader_root != shader_root_ids.end() &&
                root_ids.count(root_id) != 0u &&
                shader_root->second != root_id) {
                error("package_shader_root_signature",
                      "pipeline root signature does not match the shader record");
            }
        }
        if (instruction.opcode == "init.create_graphics_pipeline" &&
            instruction.operands[0].type == OperandType::ShaderRef &&
            instruction.operands[1].type == OperandType::ShaderRef &&
            instruction.operands[2].type == OperandType::RootSignatureRef) {
            for (std::size_t shader_index = 0u; shader_index < 2u;
                 ++shader_index) {
                const std::uint32_t shader_id =
                    instruction.operands[shader_index].u32;
                auto shader_root = shader_root_ids.find(shader_id);
                if (shader_root != shader_root_ids.end() &&
                    shader_root->second != instruction.operands[2].u32) {
                    error("package_shader_root_signature",
                          "graphics pipeline root signature does not match "
                          "its shader records");
                }
                auto target = shader_targets.find(shader_id);
                if (target != shader_targets.end()) {
                    const char* expected =
                        shader_index == 0u ? "vs_6_0" : "ps_6_0";
                    if (target->second != expected) {
                        error("package_graphics_pipeline_target",
                              "graphics pipeline shader targets must be "
                              "vs_6_0 then ps_6_0");
                    }
                }
            }
        }
        if ((instruction.opcode == "frame.set_root_signature" ||
             instruction.opcode == "frame.set_graphics_root_signature" ||
             instruction.opcode == "init.set_root_signature") &&
            instruction.operands[0].type == OperandType::RootSignatureRef &&
            root_ids.count(instruction.operands[0].u32) != 0u) {
            active_root_signature_id = instruction.operands[0].u32;
            has_active_root_signature = true;
        }
        for (std::size_t operand_index = 0;
             operand_index < instruction.operands.size(); ++operand_index) {
            const auto& operand = instruction.operands[operand_index];
            if (operand.type != contract->operands[operand_index]) {
                error("package_instruction_operand",
                      "instruction '" + instruction.opcode +
                          "' has an operand of the wrong type");
                continue;
            }
            if (operand.type == OperandType::ResourceRef &&
                resource_ids.count(operand.u32) == 0u) {
                error("package_instruction_reference",
                      "instruction references a missing resource");
            } else if (operand.type == OperandType::ShaderRef &&
                       shader_ids.count(operand.u32) == 0u) {
                error("package_instruction_reference",
                      "instruction references a missing shader");
            } else if (operand.type == OperandType::RootSignatureRef &&
                       root_ids.count(operand.u32) == 0u) {
                error("package_instruction_reference",
                      "instruction references a missing root signature");
            } else if (operand.type == OperandType::U32 &&
                       operand_index < 3u &&
                       (instruction.opcode == "frame.dispatch" ||
                        instruction.opcode == "init.dispatch") &&
                       (operand.u32 == 0u || operand.u32 > 65535u)) {
                error("package_instruction_operand",
                      "dispatch group dimension is out of range");
            } else if (operand.type == OperandType::U32 &&
                       operand_index == 0u &&
                       instruction.opcode == "frame.set_timeline" &&
                       parameter_ids.count(operand.u32) == 0u) {
                error("package_instruction_reference",
                      "timeline instruction references a missing parameter");
            } else if (operand.type == OperandType::U32 &&
                       (instruction.opcode == "frame.set_root_constants" ||
                        instruction.opcode ==
                            "frame.set_graphics_root_constants" ||
                        instruction.opcode == "init.set_root_constants") &&
                       operand_index == 2u &&
                       operand.u32 > kMaxRootSignatureDwordCost) {
                error("package_instruction_operand",
                      "root constants dword count exceeds the baseline limit");
            }
        }
        if ((instruction.opcode == "frame.dispatch" ||
             instruction.opcode == "init.dispatch") &&
            instruction.operands.size() == contract->operands.size() &&
            instruction.operands[3].type == OperandType::ShaderRef) {
            auto found = shader_targets.find(instruction.operands[3].u32);
            if (found != shader_targets.end() && found->second != "cs_6_0") {
                error("package_dispatch_shader_target",
                      "frame.dispatch requires a compute shader target");
            }
        }
        if ((instruction.opcode == "frame.set_root_constants" ||
             instruction.opcode == "frame.set_descriptor_table" ||
             instruction.opcode == "frame.set_graphics_root_constants" ||
             instruction.opcode == "frame.set_graphics_descriptor_table" ||
             instruction.opcode == "init.set_root_constants" ||
             instruction.opcode == "init.set_descriptor_table") &&
            instruction.operands.size() == contract->operands.size() &&
            instruction.operands[0].type == OperandType::U32) {
            const auto& root_operand = instruction.operands[0];
            const auto& resource_operand =
                (instruction.opcode == "frame.set_root_constants" ||
                 instruction.opcode ==
                     "frame.set_graphics_root_constants" ||
                 instruction.opcode == "init.set_root_constants")
                    ? instruction.operands[1]
                    : instruction.operands[2];
            if (resource_operand.type == OperandType::ResourceRef) {
                const ResourceDescriptor* matching = nullptr;
                for (const auto& resource : resources) {
                    if (resource.resource_id == resource_operand.u32) {
                        matching = &resource;
                        break;
                    }
                }
                if (matching != nullptr) {
                    const RootSignatureRecord* active_root = nullptr;
                    if (has_active_root_signature) {
                        auto found = root_by_id.find(active_root_signature_id);
                        if (found != root_by_id.end()) {
                            active_root = found->second;
                        }
                    }
                    if (active_root == nullptr) {
                        error("package_instruction_root_signature",
                              "instruction has no active frame.set_root_signature");
                    } else {
                        bool bound = false;
                        const bool nw06_family =
                            active_root->family.rfind("compute.nw06.", 0u) ==
                                0u ||
                            active_root->family.rfind("graphics.nw06.", 0u) ==
                                0u;
                        for (const auto& binding : active_root->bindings) {
                            const bool usage_matches =
                                binding.usage == matching->usage ||
                                (matching->usage == "uav_srv" &&
                                 (binding.usage == "uav" ||
                                  binding.usage == "srv")) ||
                                (matching->usage == "rtv_srv" &&
                                 (binding.usage == "rtv" ||
                                  binding.usage == "srv"));
                            if (binding.root_parameter == root_operand.u32 &&
                                binding.name == matching->name &&
                                usage_matches &&
                                binding.slot == matching->binding_slot &&
                                binding.space == matching->binding_space) {
                                bound = true;
                                break;
                            }
                            if (nw06_family &&
                                binding.root_parameter == root_operand.u32 &&
                                usage_matches) {
                                bound = true;
                                break;
                            }
                        }
                        if (!bound) {
                            error("package_root_signature_binding",
                                  "instruction root parameter does not bind the "
                                  "referenced resource in the active root signature");
                        }
                    }
                }
            }
        }
        if (phase == "frame" && instruction.opcode == "frame.dispatch" &&
            !instruction.operands.empty() &&
            instruction.operands.back().type == OperandType::ShaderRef) {
            const std::uint32_t shader_id = instruction.operands.back().u32;
            auto pipeline_root = pipeline_roots.find(shader_id);
            auto shader_root = shader_root_ids.find(shader_id);
            if (pipeline_root != pipeline_roots.end() &&
                shader_root != shader_root_ids.end()) {
                if (!has_active_root_signature ||
                    active_root_signature_id != pipeline_root->second ||
                    pipeline_root->second != shader_root->second) {
                    error("package_shader_root_signature",
                          "frame root signature does not match the pipeline and "
                          "shader record root signature");
                }
            }
        }
        if (instruction.opcode == "frame.set_graphics_pipeline" &&
            !instruction.operands.empty() &&
            instruction.operands[0].type == OperandType::ShaderRef) {
            auto found = shader_targets.find(instruction.operands[0].u32);
            if (found != shader_targets.end() &&
                found->second != "vs_6_0") {
                error("package_graphics_pipeline_target",
                      "frame.set_graphics_pipeline requires a vertex shader");
            }
        }
        if (instruction.opcode == "frame.execute_indirect" &&
            instruction.operands.size() == 2u &&
            instruction.operands[0].type == OperandType::ResourceRef) {
            const ResourceDescriptor* args = nullptr;
            for (const auto& resource : resources) {
                if (resource.resource_id == instruction.operands[0].u32) {
                    args = &resource;
                    break;
                }
            }
            if (args == nullptr || args->kind != "buffer" ||
                args->size_bytes < 20u || instruction.operands[1].u32 == 0u) {
                error("package_indirect_draw",
                      "frame.execute_indirect requires a DRAW_INDEXED_INSTANCED "
                      "argument buffer");
            }
        }
        if (instruction.opcode == "frame.set_render_targets" &&
            instruction.operands.size() == 2u) {
            const ResourceDescriptor* color = nullptr;
            const ResourceDescriptor* depth = nullptr;
            for (const auto& resource : resources) {
                if (resource.resource_id == instruction.operands[0].u32)
                    color = &resource;
                if (resource.resource_id == instruction.operands[1].u32)
                    depth = &resource;
            }
            if (color == nullptr || depth == nullptr ||
                color->kind != "texture2d" ||
                (color->usage != "rtv" && color->usage != "rtv_srv") ||
                depth->usage != "dsv") {
                error("package_render_targets",
                      "frame.set_render_targets requires RTV and DSV textures");
            }
        }
    }
    if (instructions.empty()) {
        AddDiagnostic(diagnostics, "package_instruction_missing",
                      "package has no " + std::string(phase) + " instructions",
                      section);
    }
    return diagnostics.size() == before;
}

bool ValidateParameters(const std::vector<ParameterRecord>& parameters,
                        std::vector<Diagnostic>& diagnostics) {
    const std::size_t before = diagnostics.size();
    std::set<std::uint32_t> ids;
    for (const auto& parameter : parameters) {
        auto error = [&](std::string code, std::string message) {
            AddDiagnostic(diagnostics, std::move(code), std::move(message),
                          std::string(kSectionParameters));
        };
        if (parameter.parameter_id == 0u) {
            error("package_parameter_id", "parameter id must be positive");
        }
        if (!ids.insert(parameter.parameter_id).second) {
            error("package_parameter_id", "duplicate parameter id");
        }
        if (parameter.name.empty()) {
            error("package_parameter_malformed", "parameter name is empty");
        }
        std::uint32_t expected = 0u;
        if (parameter.type == "float" || parameter.type == "uint" ||
            parameter.type == "int") {
            expected = 1u;
        } else if (parameter.type == "float2") {
            expected = 2u;
        } else if (parameter.type == "float3") {
            expected = 3u;
        } else if (parameter.type == "float4") {
            expected = 4u;
        } else {
            error("package_parameter_type",
                  "unknown parameter type '" + parameter.type + "'");
        }
        if (parameter.component_count != expected) {
            error("package_parameter_dimensions",
                  "parameter component count does not match its type");
        }
        if (parameter.value.size() !=
            static_cast<std::size_t>(parameter.component_count) * 4u) {
            error("package_parameter_dimensions",
                  "parameter value byte length does not match its components");
        }
        if (parameter.phase != "initialization" && parameter.phase != "frame" &&
            parameter.phase != "timeline") {
            error("package_parameter_phase", "unknown parameter phase");
        }
        if (parameter.timeline_interpolation != "none" &&
            parameter.timeline_interpolation != "linear" &&
            parameter.timeline_interpolation != "step") {
            error("package_parameter_timeline",
                  "unknown timeline interpolation");
        }
        if (parameter.timeline_start_frame > parameter.timeline_end_frame) {
            error("package_parameter_timeline",
                  "timeline start exceeds end");
        }
        if (parameter.timeline_start_frame != parameter.timeline_end_frame &&
            parameter.timeline_step == 0u) {
            error("package_parameter_timeline",
                  "timeline step must be positive");
        }
    }
    return diagnostics.size() == before;
}

bool IsKnownPassFamily(std::string_view family) {
    return family == "compute.math_noise" ||
           family == "compute.nw06.generate" ||
           family == "compute.nw06.material" ||
           family == "compute.nw06.instances" ||
           family == "compute.nw06.camera" ||
           family == "compute.nw06.light" ||
           family == "graphics.nw06.instanced" ||
           family == "compute.nw06.post";
}

bool MatchesFixedFamilyBinding(std::string_view family,
                               const RootBinding& binding) {
    if (family == "compute.nw06.generate") {
        if (binding.name == "params")
            return binding.usage == "cbv" && binding.slot == 0u;
        if (binding.name == "output")
            return binding.usage == "uav" && binding.slot == 0u;
        if (binding.name == "vertices")
            return binding.usage == "uav" && binding.slot == 1u;
        if (binding.name == "indices")
            return binding.usage == "uav" && binding.slot == 2u;
        if (binding.name == "instances")
            return binding.usage == "uav" && binding.slot == 3u;
        if (binding.name == "args")
            return binding.usage == "uav" && binding.slot == 4u;
        return false;
    }
    if (family == "compute.nw06.material") {
        if (binding.name == "params")
            return binding.usage == "cbv" && binding.slot == 0u;
        if (binding.name == "output")
            return binding.usage == "uav" && binding.slot == 0u;
        return false;
    }
    if (family == "compute.nw06.instances") {
        if (binding.name == "params")
            return binding.usage == "cbv" && binding.slot == 0u;
        if (binding.name == "output")
            return binding.usage == "uav" && binding.slot == 0u;
        if (binding.name == "args")
            return binding.usage == "uav" && binding.slot == 1u;
        return false;
    }
    if (family == "compute.nw06.camera" ||
        family == "compute.nw06.light") {
        if (binding.name == "params")
            return binding.usage == "cbv" && binding.slot == 0u;
        if (binding.name == "output")
            return binding.usage == "uav" && binding.slot == 0u;
        return false;
    }
    if (family == "graphics.nw06.instanced") {
        if (binding.name == "params")
            return binding.usage == "cbv" && binding.slot == 0u;
        if (binding.name == "texture")
            return binding.usage == "srv" && binding.slot == 0u;
        if (binding.name == "instances")
            return binding.usage == "srv" && binding.slot == 1u;
        if (binding.name == "materials")
            return binding.usage == "srv" && binding.slot == 2u;
        if (binding.name == "camera")
            return binding.usage == "srv" && binding.slot == 3u;
        if (binding.name == "light")
            return binding.usage == "srv" && binding.slot == 4u;
        if (binding.name == "depth")
            return binding.usage == "dsv" && binding.slot == 0u;
        return false;
    }
    if (family == "compute.nw06.post") {
        if (binding.name == "params")
            return binding.usage == "cbv" && binding.slot == 0u;
        if (binding.name == "input")
            return binding.usage == "srv" && binding.slot == 0u;
        if (binding.name == "bloom")
            return binding.usage == "srv" && binding.slot == 1u;
        if (binding.name == "output")
            return binding.usage == "uav" && binding.slot == 0u;
        return false;
    }
    if (family != "compute.math_noise") return false;
    if (binding.name == "params") {
        return binding.usage == "cbv" && binding.slot == 0u &&
               binding.space == 0u;
    }
    if (binding.name == "output") {
        return binding.usage == "uav" && binding.slot == 0u &&
               binding.space == 0u;
    }
    if (binding.name == "input") {
        return binding.usage == "srv" && binding.slot == 0u &&
               binding.space == 0u;
    }
    if (binding.name == "sampler") {
        return binding.usage == "sampler" && binding.slot == 0u &&
               binding.space == 0u;
    }
    return false;
}

struct SerializedRootParameter {
    std::uint32_t type = 0;
    std::uint32_t visibility = 0;
    std::uint32_t value0 = 0;
    std::uint32_t value1 = 0;
    std::uint32_t value2 = 0;
};

struct SerializedDescriptorRange {
    std::uint32_t type = 0;
    std::uint32_t count = 0;
    std::uint32_t base_register = 0;
    std::uint32_t space = 0;
    std::uint32_t offset = 0;
};

bool ParseRootSignatureBlob(
    const std::vector<std::uint8_t>& blob,
    std::vector<SerializedRootParameter>& parameters,
    std::vector<SerializedDescriptorRange>& ranges,
    std::vector<Diagnostic>& diagnostics) {
    auto malformed = [&](std::string message) {
        AddDiagnostic(diagnostics, "package_root_signature_malformed",
                      std::move(message),
                      std::string(kSectionRootSignatures));
        return false;
    };
    if (blob.size() < 44u || std::memcmp(blob.data(), "DXBC", 4u) != 0) {
        return malformed(
            "root signature blob is not a serialized DXBC signature");
    }
    const std::uint16_t major = ReadU16(blob.data() + 20);
    const std::uint16_t minor = ReadU16(blob.data() + 22);
    const std::uint32_t total_size = ReadU32(blob.data() + 24);
    const std::uint32_t chunk_count = ReadU32(blob.data() + 28);
    if (major != 1u || minor != 0u || total_size != blob.size()) {
        return malformed("serialized root signature container is malformed");
    }
    const std::uint64_t header_end =
        32u + static_cast<std::uint64_t>(chunk_count) * 4u;
    if (chunk_count == 0u || chunk_count > 64u ||
        header_end > blob.size()) {
        return malformed("serialized root signature chunk directory is invalid");
    }
    bool found = false;
    for (std::uint32_t index = 0u; index < chunk_count; ++index) {
        const std::uint32_t offset =
            ReadU32(blob.data() + 32u + index * 4u);
        const std::uint64_t offset64 = offset;
        if (offset < header_end || offset64 + 8u > blob.size()) {
            return malformed(
                "serialized root signature chunk offset is out of bounds");
        }
        const std::uint32_t chunk_size =
            ReadU32(blob.data() + offset + 4u);
        if (offset64 + 8u + chunk_size > blob.size()) {
            return malformed(
                "serialized root signature chunk size is out of bounds");
        }
        if (std::memcmp(blob.data() + offset, "RTS0", 4u) != 0) {
            continue;
        }
        if (found) {
            return malformed(
                "serialized root signature contains duplicate RTS0 chunks");
        }
        found = true;
        const std::uint8_t* payload =
            blob.data() + static_cast<std::size_t>(offset) + 8u;
        Reader reader{payload,
                      static_cast<std::size_t>(chunk_size), 0, &diagnostics,
                      std::string(kSectionRootSignatures)};
        std::uint32_t version = 0u;
        std::uint32_t parameter_count = 0u;
        std::uint32_t parameter_offset = 0u;
        std::uint32_t static_sampler_count = 0u;
        std::uint32_t static_sampler_offset = 0u;
        std::uint32_t flags = 0u;
        if (!reader.U32(version) || !reader.U32(parameter_count) ||
            !reader.U32(parameter_offset) ||
            !reader.U32(static_sampler_count) ||
            !reader.U32(static_sampler_offset) || !reader.U32(flags)) {
            return false;
        }
        if ((version != 1u && version != 2u) || flags != 0u ||
            static_sampler_count != 0u) {
            return malformed(
                "serialized root signature version/flags are unsupported");
        }
        (void)static_sampler_offset;
        if (parameter_count > kMaxRecordCount) {
            return malformed(
                "serialized root signature count exceeds the v1 limit");
        }
        const std::uint64_t parameter_bytes =
            static_cast<std::uint64_t>(parameter_count) * 12u;
        if (parameter_offset < 24u ||
            static_cast<std::uint64_t>(parameter_offset) +
                    parameter_bytes >
                chunk_size) {
            return malformed(
                "serialized root signature parameter range is invalid");
        }
        for (std::uint32_t i = 0u; i < parameter_count; ++i) {
            const std::size_t row_offset =
                static_cast<std::size_t>(parameter_offset) +
                static_cast<std::size_t>(i) * 12u;
            const std::uint32_t parameter_type =
                ReadU32(payload + row_offset);
            const std::uint32_t visibility =
                ReadU32(payload + row_offset + 4u);
            const std::uint32_t data_offset =
                ReadU32(payload + row_offset + 8u);
            SerializedRootParameter parameter;
            parameter.type = parameter_type;
            parameter.visibility = visibility;
            if (parameter_type == 1u) {
                const std::uint64_t end =
                    static_cast<std::uint64_t>(data_offset) + 12u;
                if (end > chunk_size) {
                    return malformed(
                        "serialized root constants data is out of bounds");
                }
                parameter.value0 =
                    ReadU32(payload + static_cast<std::size_t>(data_offset));
                parameter.value1 =
                    ReadU32(payload + static_cast<std::size_t>(data_offset) +
                            4u);
                parameter.value2 =
                    ReadU32(payload + static_cast<std::size_t>(data_offset) +
                            8u);
                parameters.push_back(parameter);
            } else if (parameter_type == 0u) {
                const std::uint64_t end =
                    static_cast<std::uint64_t>(data_offset) + 8u;
                if (end > chunk_size) {
                    return malformed(
                        "serialized descriptor table data is out of bounds");
                }
                const std::uint32_t range_count =
                    ReadU32(payload + static_cast<std::size_t>(data_offset));
                const std::uint32_t ranges_offset =
                    ReadU32(payload + static_cast<std::size_t>(data_offset) +
                            4u);
                if (range_count > kMaxRecordCount || ranges_offset < 24u) {
                    return malformed(
                        "serialized descriptor table range header is invalid");
                }
                parameter.value0 = static_cast<std::uint32_t>(ranges.size());
                parameter.value1 = range_count;
                parameter.value2 = 0u;
                const std::uint64_t range_base = ranges_offset;
                const std::uint64_t range_bytes =
                    static_cast<std::uint64_t>(range_count) * 20u;
                if (range_base + range_bytes > chunk_size) {
                    return malformed(
                        "serialized descriptor ranges are out of bounds");
                }
                for (std::uint32_t range_index = 0u;
                     range_index < range_count; ++range_index) {
                    const std::size_t range_offset =
                        static_cast<std::size_t>(range_base) +
                        static_cast<std::size_t>(range_index) * 20u;
                    SerializedDescriptorRange range;
                    range.type = ReadU32(payload + range_offset);
                    range.count = ReadU32(payload + range_offset + 4u);
                    range.base_register =
                        ReadU32(payload + range_offset + 8u);
                    range.space = ReadU32(payload + range_offset + 12u);
                    range.offset = ReadU32(payload + range_offset + 16u);
                    if (range.type > 3u || range.count == 0u) {
                        return malformed(
                            "serialized root signature descriptor range is invalid");
                    }
                    ranges.push_back(range);
                }
                parameters.push_back(parameter);
            } else {
                return malformed(
                    "serialized root signature uses an unsupported parameter type");
            }
        }
        // The RTS0 payload contains variable data referenced by offsets; the
        // structural checks above bound every referenced byte range.
    }
    if (!found) {
        return malformed(
            "serialized root signature is missing the RTS0 chunk");
    }
    return true;
}

bool ValidateRootSignatures(
    const std::vector<RootSignatureRecord>& records,
    const std::vector<ResourceDescriptor>& resources,
    std::vector<Diagnostic>& diagnostics) {
    const std::size_t before = diagnostics.size();
    std::set<std::uint32_t> ids;
    for (const auto& record : records) {
        auto error = [&](std::string code, std::string message) {
            AddDiagnostic(diagnostics, std::move(code), std::move(message),
                          std::string(kSectionRootSignatures));
        };
        if (record.root_signature_id == 0u) {
            error("package_root_signature_id",
                  "root signature id must be positive");
        }
        if (!ids.insert(record.root_signature_id).second) {
            error("package_root_signature_id", "duplicate root signature id");
        }
        if (!IsKnownPassFamily(record.family)) {
            error("package_pass_family_unknown",
                  "root signature uses an unknown pass family");
        } else if (record.convention != record.family + ".v1") {
            error("package_root_signature_convention",
                  "root signature convention does not match its pass family");
        }
        if (record.blob.empty()) {
            error("package_root_signature_malformed",
                  "root signature blob is empty");
        } else if (record.blob.size() > kMaxBlobBytes) {
            error("package_decode_limit",
                  "root signature blob exceeds the v1 blob size limit");
        } else {
            std::vector<SerializedRootParameter> parameters;
            std::vector<SerializedDescriptorRange> ranges;
            if (ParseRootSignatureBlob(record.blob, parameters, ranges,
                                       diagnostics)) {
                bool same_bindings =
                    parameters.size() == record.bindings.size();
                std::set<std::uint32_t> used_parameters;
                std::set<std::uint32_t> referenced_ranges;
                for (const auto& binding : record.bindings) {
                    if (!same_bindings ||
                        binding.root_parameter >= parameters.size()) {
                        same_bindings = false;
                        break;
                    }
                    used_parameters.insert(binding.root_parameter);
                    const auto& parameter =
                        parameters[binding.root_parameter];
                    if (parameter.visibility != 0u) {
                        same_bindings = false;
                        break;
                    }
                    if (binding.usage == "cbv") {
                        if (parameter.type != 1u ||
                            parameter.value0 != binding.slot ||
                            parameter.value1 != binding.space ||
                            parameter.value2 == 0u ||
                            parameter.value2 > kMaxRootSignatureDwordCost) {
                            same_bindings = false;
                            break;
                        }
                    } else {
                        std::uint32_t expected_range_type = 0u;
                        if (binding.usage == "uav") {
                            expected_range_type = 1u;
                        } else if (binding.usage == "sampler") {
                            expected_range_type = 3u;
                        } else if (binding.usage != "srv") {
                            same_bindings = false;
                            break;
                        }
                        const std::uint64_t range_offset = parameter.value0;
                        const std::uint64_t range_count = parameter.value1;
                        if (parameter.type != 0u || range_count != 1u ||
                            range_offset + range_count > ranges.size()) {
                            same_bindings = false;
                            break;
                        }
                        for (std::uint64_t range_index = range_offset;
                             range_index < range_offset + range_count;
                             ++range_index) {
                            referenced_ranges.insert(
                                static_cast<std::uint32_t>(range_index));
                            const auto& range = ranges[static_cast<
                                std::size_t>(range_index)];
                            if (range.type != expected_range_type ||
                                range.count != 1u ||
                                range.base_register != binding.slot ||
                                range.space != binding.space ||
                                range.offset != 0u) {
                                same_bindings = false;
                                break;
                            }
                        }
                        if (!same_bindings) {
                            break;
                        }
                    }
                }
                if (used_parameters.size() != parameters.size()) {
                    same_bindings = false;
                }
                if (referenced_ranges.size() != ranges.size()) {
                    same_bindings = false;
                }
                if (!same_bindings) {
                    error("package_root_signature_blob_mismatch",
                          "serialized root signature does not match the record bindings");
                }
                std::uint32_t expected_cost = 0u;
                bool cost_overflow = false;
                for (const auto& parameter : parameters) {
                    const std::uint32_t cost =
                        parameter.type == 1u ? parameter.value2 :
                        parameter.type == 0u ? 1u : 2u;
                    if (expected_cost > UINT32_MAX - cost) {
                        cost_overflow = true;
                        break;
                    }
                    expected_cost += cost;
                }
                if (cost_overflow || expected_cost != record.dword_cost) {
                    error("package_root_signature_cost",
                          "serialized root signature dword cost does not match "
                          "the record");
                }
                if (cost_overflow ||
                    expected_cost > kMaxRootSignatureDwordCost) {
                    error("package_root_signature_cost",
                          "serialized root signature dword cost exceeds the "
                          "baseline limit");
                }
            }
        }
        if (record.dword_cost > kMaxRootSignatureDwordCost) {
            error("package_root_signature_cost",
                  "root signature dword cost exceeds the baseline limit");
        }
        std::set<std::uint32_t> root_parameters;
        std::set<std::string> binding_names;
        for (const auto& binding : record.bindings) {
            if (!binding_names.insert(binding.name).second) {
                error("package_root_signature_duplicate_binding",
                      "root signature contains a duplicate binding name");
            }
            if (!root_parameters.insert(binding.root_parameter).second) {
                error("package_root_signature_root_parameter",
                      "root signature reuses a root parameter index");
            }
            if (!MatchesFixedFamilyBinding(record.family, binding)) {
                error("package_resource_binding_contract",
                      "root signature binding does not obey the fixed pass-family convention");
            }
            const ResourceDescriptor* matching = nullptr;
            for (const auto& resource : resources) {
                if ((resource.pass_family == record.family &&
                     resource.name == binding.name) ||
                    (record.family.rfind("compute.nw06.", 0u) == 0u ||
                     record.family.rfind("graphics.nw06.", 0u) == 0u)) {
                    matching = &resource;
                    if (resource.name == binding.name ||
                        resource.usage == "uav_srv" ||
                        resource.usage == "rtv_srv") {
                        break;
                    }
                }
            }
            if (matching == nullptr) {
                error("package_root_signature_binding",
                      "root signature binding has no matching resource");
            } else if (
                (record.family.rfind("compute.nw06.", 0u) != 0u &&
                 record.family.rfind("graphics.nw06.", 0u) != 0u) &&
                (matching->usage != binding.usage ||
                 matching->binding_slot != binding.slot ||
                 matching->binding_space != binding.space)) {
                error("package_root_signature_binding",
                      "root signature binding does not match its resource descriptor");
            }
        }
        if (IsKnownPassFamily(record.family) &&
            record.family.rfind("compute.nw06.", 0u) != 0u &&
            record.family.rfind("graphics.nw06.", 0u) != 0u) {
            const RootBinding* params = nullptr;
            const RootBinding* output = nullptr;
            for (const auto& binding : record.bindings) {
                if (binding.name == "params") params = &binding;
                if (binding.name == "output") output = &binding;
            }
            if (params == nullptr) {
                error("package_root_signature_binding",
                      "root signature is missing the required 'params' binding");
            } else if (params->root_parameter != 0u) {
                error("package_root_signature_root_parameter",
                      "root signature binding 'params' must use root parameter 0");
            }
            if (output == nullptr) {
                error("package_root_signature_binding",
                      "root signature is missing the required 'output' binding");
            } else if (output->root_parameter != 1u) {
                error("package_root_signature_root_parameter",
                      "root signature binding 'output' must use root parameter 1");
            }
        }
    }
    return diagnostics.size() == before;
}

bool ValidateShaders(const std::vector<ShaderRecord>& shaders,
                     const std::vector<RootSignatureRecord>& roots,
                     std::vector<Diagnostic>& diagnostics) {
    const std::size_t before = diagnostics.size();
    std::set<std::uint32_t> ids;
    std::set<std::uint32_t> root_ids;
    std::map<std::uint32_t, std::string> root_families;
    for (const auto& root : roots) root_ids.insert(root.root_signature_id);
    for (const auto& root : roots)
        root_families[root.root_signature_id] = root.family;
    for (const auto& shader : shaders) {
        auto error = [&](std::string code, std::string message) {
            AddDiagnostic(diagnostics, std::move(code), std::move(message),
                          std::string(kSectionShaders));
        };
        if (shader.shader_id == 0u) {
            error("package_shader_id", "shader id must be positive");
        }
        if (!ids.insert(shader.shader_id).second) {
            error("package_shader_id", "duplicate shader id");
        }
        if (shader.entry_point.empty()) {
            error("package_shader_malformed", "shader entry point is empty");
        } else if (!(std::isalpha(static_cast<unsigned char>(shader.entry_point[0])) ||
                     shader.entry_point[0] == '_')) {
            error("package_shader_malformed",
                  "shader entry point is not an identifier");
        }
        if (shader.target != "cs_6_0" && shader.target != "vs_6_0" &&
            shader.target != "ps_6_0") {
            error("package_shader_target_unknown",
                  "unknown shader target '" + shader.target + "'");
        }
        if (root_ids.count(shader.root_signature_id) == 0u) {
            error("package_shader_root_signature",
                  "shader references a missing root signature");
        }
        if (shader.family.empty()) {
            error("package_shader_malformed", "shader pass family is empty");
        } else if (!IsKnownPassFamily(shader.family)) {
            error("package_pass_family_unknown",
                  "shader uses an unknown pass family");
        }
        auto root_family = root_families.find(shader.root_signature_id);
        if (root_family != root_families.end() &&
            shader.family != root_family->second) {
            error("package_shader_root_signature",
                  "shader pass family does not match its root signature");
        }
        if (shader.thread_group_x == 0u || shader.thread_group_y == 0u ||
            shader.thread_group_z == 0u ||
            static_cast<std::uint64_t>(shader.thread_group_x) *
                    shader.thread_group_y * shader.thread_group_z >
                kMaxComputeThreadsPerGroup) {
            error("package_shader_dimensions",
                  "shader thread group exceeds the baseline limit");
        }
        if (shader.blob.empty()) {
            error("package_shader_malformed", "shader blob is empty");
            continue;
        }
        if (shader.blob.size() > kMaxBlobBytes) {
            error("package_decode_limit",
                  "shader blob exceeds the v1 blob size limit");
        }
        if (shader.target == "cs_6_0" || shader.target == "vs_6_0" ||
            shader.target == "ps_6_0") {
            std::string reason;
            std::string measured_model;
            if (!ValidateDxilContainer(shader.blob, reason, &measured_model)) {
                error("package_shader_payload_invalid", reason);
            } else if (measured_model != shader.target) {
                error("package_shader_target_mismatch",
                      "shader declares target '" + shader.target +
                          "' but DXIL contains '" + measured_model + "'");
            }
        }
    }
    return diagnostics.size() == before;
}

bool ChargeDecodedBlobBudget(
    const std::vector<std::uint8_t>& blob,
    std::set<std::string>& unique_blobs,
    std::uint64_t& total,
    std::string_view section,
    std::vector<Diagnostic>& diagnostics) {
    const std::string digest = Sha256Hex(blob);
    const std::uint64_t limit = static_cast<std::uint64_t>(kMaxDecodedBytes);
    if (unique_blobs.insert(digest).second) {
        if (blob.size() > limit - std::min(total, limit)) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "aggregate decoded payload exceeds the v1 memory budget",
                          std::string(section));
            return false;
        }
        total += blob.size();
    }
    if (blob.size() > limit - std::min(total, limit)) {
        AddDiagnostic(diagnostics, "package_decode_limit",
                      "aggregate decoded payload exceeds the v1 memory budget",
                      std::string(section));
        return false;
    }
    total += blob.size();
    return true;
}

bool ValidateCrossReferences(const CapabilityRecord& capability,
                             const std::vector<Instruction>& initialization,
                             const std::vector<Instruction>& frame,
                             const std::vector<ShaderRecord>& shaders,
                             std::vector<Diagnostic>& diagnostics) {
    const std::size_t before = diagnostics.size();
    std::set<std::string> init_set;
    std::set<std::string> frame_set;
    std::set<std::string> targets;
    for (const auto& instruction : initialization) init_set.insert(instruction.opcode);
    for (const auto& instruction : frame) frame_set.insert(instruction.opcode);
    for (const auto& shader : shaders) targets.insert(shader.target);
    if (std::vector<std::string>(init_set.begin(), init_set.end()) !=
        capability.required_init_instructions) {
        AddDiagnostic(diagnostics, "package_capability_instruction_mismatch",
                      "CAP required_init_instructions do not match emitted instructions",
                      std::string(kSectionCapability));
    }
    if (std::vector<std::string>(frame_set.begin(), frame_set.end()) !=
        capability.required_frame_instructions) {
        AddDiagnostic(diagnostics, "package_capability_instruction_mismatch",
                      "CAP required_frame_instructions do not match emitted instructions",
                      std::string(kSectionCapability));
    }
    if (std::vector<std::string>(targets.begin(), targets.end()) !=
        capability.emitted_shader_targets) {
        AddDiagnostic(diagnostics,
                      "package_capability_shader_target_mismatch",
                      "CAP emitted_shader_targets do not match emitted shaders",
                      std::string(kSectionCapability));
    }
    std::set<std::uint32_t> pipeline_shaders;
    std::set<std::uint32_t> dispatched_shaders;
    std::set<std::uint32_t> init_dispatched_shaders;
    for (const auto& instruction : initialization) {
        if (instruction.opcode == "init.create_pipeline" &&
            !instruction.operands.empty() &&
            instruction.operands[0].type == OperandType::ShaderRef) {
            pipeline_shaders.insert(instruction.operands[0].u32);
        }
        if (instruction.opcode == "init.create_graphics_pipeline") {
            for (std::size_t index = 0u;
                 index < 2u && index < instruction.operands.size(); ++index) {
                if (instruction.operands[index].type ==
                    OperandType::ShaderRef) {
                    pipeline_shaders.insert(instruction.operands[index].u32);
                }
            }
        } else if (IsKnownPassFamily(record.family)) {
            const RootBinding* params = nullptr;
            for (const auto& binding : record.bindings) {
                if (binding.name == "params") {
                    params = &binding;
                    break;
                }
            }
            if (params == nullptr || params->root_parameter != 0u) {
                error("package_root_signature_binding",
                      "NW-06 root signatures require a 'params' root constant "
                      "binding at root parameter 0");
            }
        }
        if (instruction.opcode == "init.dispatch" &&
            !instruction.operands.empty() &&
            instruction.operands.back().type == OperandType::ShaderRef) {
            init_dispatched_shaders.insert(instruction.operands.back().u32);
        }
    }
    for (const auto& instruction : frame) {
        if (instruction.opcode == "frame.dispatch" &&
            !instruction.operands.empty() &&
            instruction.operands.back().type == OperandType::ShaderRef) {
            dispatched_shaders.insert(instruction.operands.back().u32);
        }
    }
    for (const auto& shader : shaders) {
        if (pipeline_shaders.count(shader.shader_id) == 0u) {
            AddDiagnostic(diagnostics, "package_shader_unreferenced",
                          "shader has no init.create_pipeline instruction",
                          std::string(kSectionInitialization));
        }
        if (shader.target == "cs_6_0" &&
            dispatched_shaders.count(shader.shader_id) == 0u &&
            init_dispatched_shaders.count(shader.shader_id) == 0u) {
            AddDiagnostic(diagnostics, "package_shader_unreferenced",
                          "shader has no frame.dispatch instruction",
                          std::string(kSectionFrame));
        }
    }
    return diagnostics.size() == before;
}

} // namespace

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

std::string Sha256Hex(const std::uint8_t* data, std::size_t size) {
    Sha256 sha;
    sha.Update(data, size);
    const auto digest = sha.Final();
    return Hex(digest.data(), digest.size());
}

std::string Sha256Hex(std::string_view text) {
    return Sha256Hex(reinterpret_cast<const std::uint8_t*>(text.data()),
                     text.size());
}

std::string Sha256Hex(const std::vector<std::uint8_t>& bytes) {
    return Sha256Hex(bytes.data(), bytes.size());
}

std::string BytesToHex(const std::uint8_t* data, std::size_t size) {
    return Hex(data, size);
}

std::string DiagnosticToJson(const Diagnostic& diagnostic) {
    std::ostringstream out;
    out << "{\"category\":\"package_validation\",\"severity\":\"error\","
           "\"code\":"
        << JsonEscape(diagnostic.code) << ",\"message\":"
        << JsonEscape(diagnostic.message);
    if (!diagnostic.section.empty()) {
        out << ",\"section\":" << JsonEscape(diagnostic.section);
    }
    if (diagnostic.has_offset) {
        out << ",\"offset\":" << diagnostic.offset;
    }
    out << "}";
    return out.str();
}

std::string_view ToString(OperandType type) noexcept {
    switch (type) {
    case OperandType::U32: return "u32";
    case OperandType::U64: return "u64";
    case OperandType::I64: return "i64";
    case OperandType::Bool: return "bool";
    case OperandType::String: return "string";
    case OperandType::F32: return "f32";
    case OperandType::ResourceRef: return "resource_ref";
    case OperandType::ShaderRef: return "shader_ref";
    case OperandType::RootSignatureRef: return "root_signature_ref";
    }
    return "unknown";
}

std::optional<OperandType> ParseOperandType(std::string_view text) noexcept {
    for (OperandType type :
         {OperandType::U32, OperandType::U64, OperandType::I64,
          OperandType::Bool, OperandType::String, OperandType::F32,
          OperandType::ResourceRef, OperandType::ShaderRef,
          OperandType::RootSignatureRef}) {
        if (ToString(type) == text) return type;
    }
    return std::nullopt;
}

std::string ComputeProfileFingerprint(const CapabilityRecord& capability) {
    return "sha256:v1:" + Sha256Hex(CanonicalCapabilityJson(capability));
}

std::vector<std::uint8_t> MakeRootSignatureBlob(
    std::string_view family, const std::vector<RootBinding>& bindings) {
    (void)family;
    std::vector<RootBinding> ordered = bindings;
    std::stable_sort(ordered.begin(), ordered.end(),
                     [](const RootBinding& a, const RootBinding& b) {
                         return a.root_parameter < b.root_parameter;
                     });
    const std::uint32_t header_size = 24u;
    const std::uint32_t parameter_size = 12u;
    const std::uint32_t data_start =
        header_size +
        parameter_size * static_cast<std::uint32_t>(ordered.size());
    Writer data;
    struct ParameterRow {
        std::uint32_t type = 0;
        std::uint32_t visibility = 0;
        std::uint32_t data_offset = 0;
    };
    std::vector<ParameterRow> parameters;
    for (const auto& binding : ordered) {
        const std::uint32_t data_offset =
            data_start + static_cast<std::uint32_t>(data.bytes.size());
        if (binding.usage == "cbv") {
            parameters.push_back(ParameterRow{1u, 0u, data_offset});
            data.U32(binding.slot);
            data.U32(binding.space);
            data.U32(4u);
            continue;
        }
        std::uint32_t range_type = 0u;
        if (binding.usage == "uav") {
            range_type = 1u;
        } else if (binding.usage == "sampler") {
            range_type = 3u;
        } else if (binding.usage != "srv") {
            return {};
        }
        parameters.push_back(ParameterRow{0u, 0u, data_offset});
        data.U32(1u); // descriptor range count
        data.U32(data_offset + 8u); // absolute RTS0-payload range offset
        data.U32(range_type);
        data.U32(1u);
        data.U32(binding.slot);
        data.U32(binding.space);
        data.U32(0u);
    }
    Writer payload;
    payload.U32(1u); // serialized signature version
    payload.U32(static_cast<std::uint32_t>(parameters.size()));
    payload.U32(header_size);
    payload.U32(0u); // static sampler count
    payload.U32(data_start + static_cast<std::uint32_t>(data.bytes.size()));
    payload.U32(0u); // flags
    for (const auto& parameter : parameters) {
        payload.U32(parameter.type);
        payload.U32(parameter.visibility);
        payload.U32(parameter.data_offset);
    }
    payload.bytes.insert(payload.bytes.end(), data.bytes.begin(),
                         data.bytes.end());
    Sha256 sha;
    sha.Update(payload.bytes.data(), payload.bytes.size());
    const auto digest = sha.Final();
    Writer writer;
    writer.U8('D');
    writer.U8('X');
    writer.U8('B');
    writer.U8('C');
    writer.bytes.insert(writer.bytes.end(), digest.begin(),
                        digest.begin() + 16);
    writer.U16(1u);
    writer.U16(0u);
    const std::uint32_t total_size =
        static_cast<std::uint32_t>(32u + 4u + 8u + payload.bytes.size());
    writer.U32(total_size);
    writer.U32(1u);
    writer.U32(36u);
    writer.U8('R');
    writer.U8('T');
    writer.U8('S');
    writer.U8('0');
    writer.U32(static_cast<std::uint32_t>(payload.bytes.size()));
    writer.bytes.insert(writer.bytes.end(), payload.bytes.begin(),
                        payload.bytes.end());
    return writer.ok ? writer.bytes : std::vector<std::uint8_t>{};
}

bool ValidatePackage(const Package& package,
                     std::vector<Diagnostic>& diagnostics,
                     bool allow_dev) {
    ValidateCapability(package.capability, package.resources,
                       package.initialization, package.frame, package.shaders,
                       package.root_signatures, diagnostics,
                       (package.flags & 1u) != 0u);
    ValidateResources(package.resources, diagnostics);
    ValidateTransientBindingReuse(package.resources, package.frame,
                                  diagnostics);
    ValidateParameters(package.parameters, diagnostics);
    ValidateRootSignatures(package.root_signatures, package.resources,
                           diagnostics);
    ValidateShaders(package.shaders, package.root_signatures, diagnostics);
    {
        std::uint64_t decoded_total = 0u;
        bool budget_ok = true;
        std::set<std::string> unique_root_blobs;
        for (const auto& root : package.root_signatures) {
            if (!ChargeDecodedBlobBudget(root.blob, unique_root_blobs,
                                         decoded_total,
                                         kSectionRootSignatures, diagnostics)) {
                budget_ok = false;
                break;
            }
        }
        if (budget_ok) {
            std::set<std::string> unique_shader_blobs;
            for (const auto& shader : package.shaders) {
                if (!ChargeDecodedBlobBudget(
                        shader.blob, unique_shader_blobs, decoded_total,
                        kSectionShaders, diagnostics)) {
                    break;
                }
            }
        }
    }
    if ((package.flags & 1u) != 0u) {
        for (const auto& shader : package.shaders) {
            std::string reason;
            if (HasForbiddenDxilMetadataChunks(shader.blob, reason)) {
                AddDiagnostic(diagnostics, "package_shader_payload_invalid",
                              "shader " + std::to_string(shader.shader_id) +
                                  " " + reason,
                              std::string(kSectionShaders));
            }
        }
    }
    std::set<std::uint32_t> resource_ids;
    std::set<std::uint32_t> shader_ids;
    std::set<std::uint32_t> root_ids;
    std::set<std::uint32_t> parameter_ids;
    for (const auto& resource : package.resources)
        resource_ids.insert(resource.resource_id);
    for (const auto& shader : package.shaders)
        shader_ids.insert(shader.shader_id);
    for (const auto& root : package.root_signatures)
        root_ids.insert(root.root_signature_id);
    for (const auto& parameter : package.parameters)
        parameter_ids.insert(parameter.parameter_id);
    std::map<std::uint32_t, std::uint32_t> pipeline_roots;
    for (const auto& instruction : package.initialization) {
        if (instruction.opcode == "init.create_pipeline" &&
            instruction.operands.size() == 2u &&
            instruction.operands[0].type == OperandType::ShaderRef &&
            instruction.operands[1].type == OperandType::RootSignatureRef) {
            pipeline_roots[instruction.operands[0].u32] =
                instruction.operands[1].u32;
        }
        if (instruction.opcode == "init.create_graphics_pipeline" &&
            instruction.operands.size() == 3u &&
            instruction.operands[2].type == OperandType::RootSignatureRef) {
            pipeline_roots[instruction.operands[0].u32] =
                instruction.operands[2].u32;
            pipeline_roots[instruction.operands[1].u32] =
                instruction.operands[2].u32;
        }
    }
    std::set<std::string> init_opcodes;
    std::set<std::string> frame_opcodes;
    ValidateInstructions(package.initialization, "initialization",
                         package.resources, package.root_signatures,
                         package.shaders, pipeline_roots,
                         resource_ids, shader_ids, root_ids, parameter_ids,
                         init_opcodes, diagnostics);
    ValidateInstructions(package.frame, "frame", package.resources,
                         package.root_signatures, package.shaders,
                         pipeline_roots,
                         resource_ids, shader_ids, root_ids, parameter_ids,
                         frame_opcodes, diagnostics);
    ValidateCrossReferences(package.capability, package.initialization,
                            package.frame, package.shaders, diagnostics);
    if (package.dev_metadata.has_value() && (package.flags & 1u) != 0u) {
        AddDiagnostic(diagnostics, "package_release_authoring_metadata",
                      "release packages must not contain development metadata",
                      std::string(kSectionDev));
    }
    if (package.dev_metadata.has_value() && !allow_dev) {
        // DecodePackage owns the release-mode rejection; ValidatePackage
        // accepts the model only when the caller explicitly allows dev data.
        AddDiagnostic(diagnostics, "package_section_forbidden",
                      "development metadata is not allowed in a release package",
                      std::string(kSectionDev));
    }
    return diagnostics.empty();
}

std::vector<std::uint8_t> EncodePackage(
    const Package& package, std::vector<Diagnostic>& diagnostics) {
    if (package.capability.profile_fingerprint.empty()) {
        Package copy = package;
        copy.capability.profile_fingerprint =
            ComputeProfileFingerprint(copy.capability);
        return EncodePackage(copy, diagnostics);
    }
    if (!ValidatePackage(package, diagnostics, true)) {
        return {};
    }
    std::map<std::string, std::vector<std::uint8_t>> sections;
    sections[std::string(kSectionCapability)] = EncodeCapability(package.capability);
    sections[std::string(kSectionResources)] = EncodeResources(package.resources);
    sections[std::string(kSectionInitialization)] =
        EncodeInstructions(package.initialization);
    sections[std::string(kSectionFrame)] = EncodeInstructions(package.frame);
    sections[std::string(kSectionParameters)] = EncodeParameters(package.parameters);
    sections[std::string(kSectionRootSignatures)] =
        EncodeRootSignatures(package.root_signatures);
    sections[std::string(kSectionShaders)] = EncodeShaders(package.shaders);
    if (package.dev_metadata.has_value()) {
        sections[std::string(kSectionDev)] = *package.dev_metadata;
    }
    std::map<std::string, std::uint32_t> record_counts;
    record_counts[std::string(kSectionCapability)] = 1u;
    record_counts[std::string(kSectionResources)] =
        static_cast<std::uint32_t>(package.resources.size());
    record_counts[std::string(kSectionInitialization)] =
        static_cast<std::uint32_t>(package.initialization.size());
    record_counts[std::string(kSectionFrame)] =
        static_cast<std::uint32_t>(package.frame.size());
    record_counts[std::string(kSectionParameters)] =
        static_cast<std::uint32_t>(package.parameters.size());
    record_counts[std::string(kSectionRootSignatures)] =
        static_cast<std::uint32_t>(package.root_signatures.size());
    record_counts[std::string(kSectionShaders)] =
        static_cast<std::uint32_t>(package.shaders.size());
    if (package.dev_metadata.has_value()) {
        record_counts[std::string(kSectionDev)] = 0u;
    }
    for (const auto& [name, payload] : sections) {
        if (name != std::string(kSectionDev) && payload.empty()) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "section '" + name +
                              "' exceeds the v1 writer limits");
            return {};
        }
        if (payload.size() > kMaxSectionBytes) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "section '" + name +
                              "' exceeds the v1 section size limit");
            return {};
        }
    }
    for (const auto& [name, count] : record_counts) {
        if (count > kMaxRecordCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "section '" + name +
                              "' exceeds the v1 record count limit");
            return {};
        }
    }
    const std::uint64_t directory_end =
        kHeaderSize + static_cast<std::uint64_t>(kSectionEntrySize) *
                          sections.size();
    std::uint64_t cursor = directory_end;
    std::vector<std::tuple<std::string, std::uint64_t, std::uint64_t>> entries;
    for (const auto& [name, payload] : sections) {
        entries.emplace_back(name, cursor, payload.size());
        cursor += payload.size();
    }
    const std::uint64_t checksum_offset = cursor;
    const std::uint64_t total_size = cursor + kChecksumSize;
    if (total_size > kMaxPackageBytes) {
        AddDiagnostic(diagnostics, "package_decode_limit",
                      "package size exceeds the configured limit");
        return {};
    }
    Writer writer;
    writer.bytes.insert(writer.bytes.end(), kMagic.begin(), kMagic.end());
    writer.U16(kVersionMajor);
    writer.U16(kVersionMinor);
    writer.U32(kHeaderSize);
    writer.U32(package.flags);
    writer.U32(0);
    writer.U64(kHeaderSize);
    writer.U64(total_size);
    writer.U32(static_cast<std::uint32_t>(sections.size()));
    writer.U32(kSectionEntrySize);
    writer.U64(checksum_offset);
    writer.U64(0);
    for (const auto& [name, offset, size] : entries) {
        writer.bytes.insert(writer.bytes.end(), name.begin(), name.end());
        writer.U32(0);
        writer.U32(record_counts.at(name));
        writer.U64(offset);
        writer.U64(size);
        writer.U32(0);
    }
    for (const auto& entry : entries) {
        const auto& payload = sections.at(std::get<0>(entry));
        writer.bytes.insert(writer.bytes.end(), payload.begin(), payload.end());
    }
    const std::string checksum = Sha256Hex(writer.bytes);
    for (std::size_t i = 0; i < checksum.size(); i += 2u) {
        const std::string byte = checksum.substr(i, 2);
        writer.U8(static_cast<std::uint8_t>(std::stoul(byte, nullptr, 16)));
    }
    return writer.bytes;
}

bool DecodePackage(const std::uint8_t* data, std::size_t size_bytes,
                   Package& package, std::vector<Diagnostic>& diagnostics,
                   bool allow_dev, std::size_t max_total_bytes,
                   std::size_t max_section_bytes, bool verify_integrity) {
    package = Package{};
    if (size_bytes < kHeaderSize + kChecksumSize) {
        AddDiagnostic(diagnostics, "package_truncated",
                      "package is smaller than header and checksum");
        return false;
    }
    if (size_bytes > max_total_bytes) {
        AddDiagnostic(diagnostics, "package_decode_limit",
                      "package exceeds configured size limit");
        return false;
    }
    if (std::memcmp(data, kMagic.data(), kMagic.size()) != 0) {
        AddDiagnostic(diagnostics, "package_header_magic", "bad package magic", {},
                      0, true);
        return false;
    }
    const std::uint16_t major = ReadU16(data + 8);
    const std::uint16_t minor = ReadU16(data + 10);
    const std::uint32_t header_size = ReadU32(data + 12);
    const std::uint32_t flags = ReadU32(data + 16);
    const std::uint32_t reserved = ReadU32(data + 20);
    const std::uint64_t directory_offset = ReadU64(data + 24);
    const std::uint64_t total_size = ReadU64(data + 32);
    const std::uint32_t section_count = ReadU32(data + 40);
    const std::uint32_t entry_size = ReadU32(data + 44);
    const std::uint64_t checksum_offset = ReadU64(data + 48);
    const std::uint64_t reserved2 = ReadU64(data + 56);
    if (major != kVersionMajor || minor != kVersionMinor) {
        AddDiagnostic(diagnostics, "package_version_unknown",
                      "unknown .nwp version", {}, 8, true);
        return false;
    }
    if (header_size != kHeaderSize) {
        AddDiagnostic(diagnostics, "package_header_size",
                      "header_size is not 64", {}, 12, true);
        return false;
    }
    if (reserved != 0u || reserved2 != 0u) {
        AddDiagnostic(diagnostics, "package_header_reserved",
                      "reserved header fields must be zero", {}, 16, true);
        return false;
    }
    if (directory_offset != kHeaderSize) {
        AddDiagnostic(diagnostics, "package_directory_offset",
                      "section directory offset is not 64", {}, 24, true);
        return false;
    }
    if (total_size != size_bytes) {
        AddDiagnostic(diagnostics, "package_size_mismatch",
                      "header total_size does not match actual size", {}, 32,
                      true);
        return false;
    }
    if (entry_size != kSectionEntrySize) {
        AddDiagnostic(diagnostics, "package_directory_entry_size",
                      "section directory entry size is not 32", {}, 44, true);
        return false;
    }
    if (section_count == 0u || section_count > kMaxSectionCount) {
        AddDiagnostic(diagnostics, "package_section_count",
                      "section count is out of range", {}, 40, true);
        return false;
    }
    const std::uint64_t directory_end =
        kHeaderSize + static_cast<std::uint64_t>(section_count) * kSectionEntrySize;
    if (directory_end > size_bytes - kChecksumSize) {
        AddDiagnostic(diagnostics, "package_truncated",
                      "section directory exceeds package bounds");
        return false;
    }
    if (checksum_offset != size_bytes - kChecksumSize) {
        AddDiagnostic(diagnostics, "package_checksum_offset",
                      "checksum offset is not total_size - 32", {}, 48, true);
        return false;
    }
    if (verify_integrity) {
        const std::string expected = Sha256Hex(data, checksum_offset);
        const std::string actual = Hex(data + checksum_offset, kChecksumSize);
        if (expected != actual) {
            AddDiagnostic(diagnostics, "package_checksum_mismatch",
                          "package integrity checksum does not match",
                          {}, checksum_offset, true);
            return false;
        }
    }
    struct Entry {
        std::string name;
        std::uint64_t offset;
        std::uint64_t size;
        std::uint64_t entry_offset;
        std::uint32_t record_count;
    };
    std::vector<Entry> entries;
    std::set<std::string> names;
    std::string previous_section_name;
    for (std::uint32_t index = 0; index < section_count; ++index) {
        const std::uint64_t entry_offset =
            kHeaderSize + static_cast<std::uint64_t>(index) * kSectionEntrySize;
        const std::uint8_t* raw = data + entry_offset;
        const std::string name(reinterpret_cast<const char*>(raw), 4);
        const std::uint32_t entry_flags = ReadU32(raw + 4);
        const std::uint32_t record_count = ReadU32(raw + 8);
        const std::uint64_t offset = ReadU64(raw + 12);
        const std::uint64_t section_size = ReadU64(raw + 20);
        const std::uint32_t entry_reserved = ReadU32(raw + 28);
        if (name != std::string(kSectionCapability) &&
            name != std::string(kSectionResources) &&
            name != std::string(kSectionInitialization) &&
            name != std::string(kSectionFrame) &&
            name != std::string(kSectionParameters) &&
            name != std::string(kSectionShaders) &&
            name != std::string(kSectionRootSignatures) &&
            name != std::string(kSectionDev)) {
            AddDiagnostic(diagnostics, "package_section_unknown",
                          "unknown section '" + name + "'", name, entry_offset,
                          true);
            return false;
        }
        if (name == std::string(kSectionDev) && !allow_dev) {
            AddDiagnostic(diagnostics, "package_section_forbidden",
                          "development metadata section is not allowed in release",
                          name, entry_offset, true);
            return false;
        }
        if (!names.insert(name).second) {
            AddDiagnostic(diagnostics, "package_section_duplicate",
                          "duplicate section '" + name + "'", name,
                          entry_offset, true);
            return false;
        }
        if (!previous_section_name.empty() &&
            name <= previous_section_name) {
            AddDiagnostic(diagnostics, "package_section_order",
                          "section directory is not in canonical ascending order",
                          name, entry_offset, true);
            return false;
        }
        previous_section_name = name;
        if (entry_flags != 0u || entry_reserved != 0u) {
            AddDiagnostic(diagnostics, "package_section_flags",
                          "section flags/reserved bits are unsupported", name,
                          entry_offset, true);
            return false;
        }
        if (section_size > max_section_bytes) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "section size exceeds configured limit", name,
                          entry_offset, true);
            return false;
        }
        if (offset < directory_end || offset > checksum_offset ||
            section_size > checksum_offset - offset) {
            AddDiagnostic(diagnostics, "package_section_range",
                          "section range is outside the payload region", name,
                          entry_offset, true);
            return false;
        }
        entries.push_back(Entry{name, offset, section_size, entry_offset,
                                record_count});
    }
    const std::vector<std::string> required = {
        std::string(kSectionCapability), std::string(kSectionResources),
        std::string(kSectionInitialization), std::string(kSectionFrame),
        std::string(kSectionParameters),
        std::string(kSectionRootSignatures), std::string(kSectionShaders)};
    for (const auto& name : required) {
        if (names.count(name) == 0u) {
            AddDiagnostic(diagnostics, "package_section_missing",
                          "missing required section '" + name + "'");
            return false;
        }
    }
    // Canonical v1 payloads are packed contiguously in directory order.  A
    // checksum-correct package with inserted padding before a section must not
    // be accepted and then re-written differently on round-trip.
    std::uint64_t previous_end = directory_end;
    for (const auto& entry : entries) {
        if (entry.offset < previous_end) {
            AddDiagnostic(diagnostics, "package_section_overlap",
                          "section '" + entry.name + "' overlaps another", entry.name,
                          entry.entry_offset, true);
            return false;
        }
        if (entry.offset > previous_end) {
            AddDiagnostic(diagnostics, "package_section_gap",
                          "section '" + entry.name +
                              "' is not contiguous with the previous payload",
                          entry.name, entry.entry_offset, true);
            return false;
        }
        previous_end = entry.offset + entry.size;
    }
    if (previous_end != checksum_offset) {
        AddDiagnostic(diagnostics, "package_section_gap",
                      "section payloads do not end at the checksum");
        return false;
    }
    std::map<std::string, const std::uint8_t*> payloads;
    std::map<std::string, std::size_t> payload_sizes;
    for (const auto& entry : entries) {
        payloads[entry.name] = data + entry.offset;
        payload_sizes[entry.name] = static_cast<std::size_t>(entry.size);
    }
    // Decode CAP.
    {
        Reader reader{payloads[std::string(kSectionCapability)],
                      payload_sizes[std::string(kSectionCapability)], 0, &diagnostics,
                      std::string(kSectionCapability)};
        std::uint16_t version = 0;
        std::uint32_t count = 0;
        if (!reader.U16(version)) return false;
        if (version != 1u) {
            AddDiagnostic(diagnostics, "package_unknown_section_version",
                          "CAP section version is not supported",
                          std::string(kSectionCapability));
            return false;
        }
        std::uint16_t section_flags = 0;
        if (!reader.U16(section_flags)) return false;
        if (section_flags != 0u) {
            AddDiagnostic(diagnostics, "package_capability_flags",
                          "CAP section flags must be zero in v1",
                          std::string(kSectionCapability));
            return false;
        }
        if (!reader.U32(count)) return false;
        if (count > kMaxFieldCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "CAP field count exceeds limit",
                          std::string(kSectionCapability));
            return false;
        }
        const auto fields = CapabilityFields();
        std::set<std::string> seen_fields;
        for (std::uint32_t i = 0; i < count; ++i) {
            std::string name;
            std::uint8_t type_code = 0;
            if (!reader.String(name) || !reader.U8(type_code)) return false;
            auto found = fields.find(name);
            if (found == fields.end()) {
                AddDiagnostic(diagnostics, "package_capability_unknown_field",
                              "unknown CAP field '" + name + "'",
                              std::string(kSectionCapability));
                return false;
            }
            if (static_cast<FieldType>(type_code) != found->second) {
                AddDiagnostic(diagnostics, "package_capability_field_type",
                              "CAP field type mismatch for '" + name + "'",
                              std::string(kSectionCapability));
                return false;
            }
            if (!seen_fields.insert(name).second) {
                AddDiagnostic(diagnostics, "package_capability_duplicate_field",
                              "duplicate CAP field '" + name + "'",
                              std::string(kSectionCapability));
                return false;
            }
            auto& cap = package.capability;
            if (name == "profile_id") {
                if (!reader.String(cap.profile_id)) return false;
            } else if (name == "requirements_digest") {
                if (!reader.String(cap.requirements_digest)) return false;
            } else if (name == "optional_profile_id") {
                if (!reader.String(cap.optional_profile_id)) return false;
            } else if (name == "optional_requirements_digest") {
                if (!reader.String(cap.optional_requirements_digest)) return false;
            } else if (name == "optional_profile_separate_artifact") {
                if (!reader.Bool(cap.optional_profile_separate_artifact)) return false;
            } else if (name == "feature_level") {
                if (!reader.U32(cap.feature_level)) return false;
            } else if (name == "shader_model") {
                if (!reader.U32(cap.shader_model)) return false;
            } else if (name == "shader_model_text") {
                if (!reader.String(cap.shader_model_text)) return false;
            } else if (name == "resource_binding_tier") {
                if (!reader.U32(cap.resource_binding_tier)) return false;
            } else if (name == "resource_heap_tier") {
                if (!reader.U32(cap.resource_heap_tier)) return false;
            } else if (name == "direct_queue_count") {
                if (!reader.U32(cap.direct_queue_count)) return false;
            } else if (name == "max_cbv_srv_uav_descriptors_per_frame") {
                if (!reader.U32(cap.max_cbv_srv_uav_descriptors_per_frame)) return false;
            } else if (name == "max_sampler_descriptors_per_frame") {
                if (!reader.U32(cap.max_sampler_descriptors_per_frame)) return false;
            } else if (name == "max_root_signature_dword_cost") {
                if (!reader.U32(cap.max_root_signature_dword_cost)) return false;
            } else if (name == "min_windows_build") {
                if (!reader.U32(cap.min_windows_build)) return false;
            } else if (name == "required_dxgi_formats") {
                if (!reader.StringList(cap.required_dxgi_formats)) return false;
            } else if (name == "limit_max_texture_2d_dimension") {
                if (!reader.U32(cap.limit_max_texture_2d_dimension)) return false;
            } else if (name == "limit_max_compute_threads_per_group") {
                if (!reader.U32(cap.limit_max_compute_threads_per_group)) return false;
            } else if (name == "limit_max_buffer_bytes") {
                if (!reader.U64(cap.limit_max_buffer_bytes)) return false;
            } else if (name == "limit_swap_chain_buffer_count") {
                if (!reader.U32(cap.limit_swap_chain_buffer_count)) return false;
            } else if (name == "emitted_shader_targets") {
                if (!reader.StringList(cap.emitted_shader_targets)) return false;
            } else if (name == "runtime_dxc_dependency") {
                if (!reader.Bool(cap.runtime_dxc_dependency)) return false;
            } else if (name == "runtime_slang_dependency") {
                if (!reader.Bool(cap.runtime_slang_dependency)) return false;
            } else if (name == "agility_sdk_required") {
                if (!reader.Bool(cap.agility_sdk_required)) return false;
            } else if (name == "toolchain_dxc_version") {
                if (!reader.String(cap.toolchain_dxc_version)) return false;
            } else if (name == "toolchain_identity") {
                if (!reader.String(cap.toolchain_identity)) return false;
            } else if (name == "compiler_fingerprint") {
                if (!reader.String(cap.compiler_fingerprint)) return false;
            } else if (name == "profile_fingerprint") {
                if (!reader.String(cap.profile_fingerprint)) return false;
            } else if (name == "required_init_instructions") {
                if (!reader.StringList(cap.required_init_instructions)) return false;
            } else if (name == "required_frame_instructions") {
                if (!reader.StringList(cap.required_frame_instructions)) return false;
            }
        }
        if (!reader.AtEnd()) {
            AddDiagnostic(diagnostics, "package_trailing_data",
                          "CAP section has trailing bytes",
                          std::string(kSectionCapability));
            return false;
        }
        for (const auto& field_entry : fields) {
            const std::string& name = field_entry.first;
            if (seen_fields.count(name) == 0u) {
                AddDiagnostic(diagnostics, "package_capability_missing_field",
                              "CAP section is missing required field '" + name + "'",
                              std::string(kSectionCapability));
                return false;
            }
        }
    }
    // Reject unknown profiles/digests before decoding bulk payloads.  The full
    // capability contract is checked again after the package is decoded.
    if (!ValidateCapabilityStatic(
            package.capability, diagnostics, (flags & 1u) != 0u)) {
        return false;
    }
    // Decode RES.
    {
        Reader reader{payloads[std::string(kSectionResources)],
                      payload_sizes[std::string(kSectionResources)], 0, &diagnostics,
                      std::string(kSectionResources)};
        std::uint16_t version = 0;
        std::uint32_t count = 0;
        if (!reader.U16(version)) return false;
        if (version != 1u) {
            AddDiagnostic(diagnostics, "package_unknown_section_version",
                          "RES section version is not supported",
                          std::string(kSectionResources));
            return false;
        }
        if (!reader.U32(count)) return false;
        if (count > kMaxRecordCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "RES record count exceeds limit",
                          std::string(kSectionResources));
            return false;
        }
        for (std::uint32_t i = 0; i < count; ++i) {
            ResourceDescriptor resource;
            if (!reader.U32(resource.resource_id) ||
                !reader.String(resource.name) || !reader.String(resource.kind) ||
                !reader.String(resource.format) ||
                !reader.String(resource.usage) ||
                !reader.String(resource.pass_family) ||
                !reader.U32(resource.binding_slot) ||
                !reader.U32(resource.binding_space) ||
                !reader.U32(resource.width) || !reader.U32(resource.height) ||
                !reader.U32(resource.depth) || !reader.U32(resource.array_size) ||
                !reader.U32(resource.mip_levels) ||
                !reader.U64(resource.size_bytes) || !reader.U32(resource.flags)) {
                return false;
            }
            package.resources.push_back(std::move(resource));
        }
        if (!reader.AtEnd()) {
            AddDiagnostic(diagnostics, "package_trailing_data",
                          "RES section has trailing bytes",
                          std::string(kSectionResources));
            return false;
        }
    }
    if (!ValidateResources(package.resources, diagnostics)) {
        return false;
    }
    const auto decode_instructions = [&](std::string_view section,
                                         std::vector<Instruction>& output) {
        Reader reader{payloads[std::string(section)],
                      payload_sizes[std::string(section)], 0, &diagnostics,
                      std::string(section)};
        std::uint16_t version = 0;
        std::uint32_t count = 0;
        if (!reader.U16(version) || !reader.U32(count)) return false;
        if (version != 1u) {
            AddDiagnostic(diagnostics, "package_unknown_section_version",
                          "instruction section version is not supported",
                          std::string(section));
            return false;
        }
        if (count > kMaxRecordCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "instruction record count exceeds limit",
                          std::string(section));
            return false;
        }
        for (std::uint32_t i = 0; i < count; ++i) {
            Instruction instruction;
            std::uint32_t operand_count = 0;
            if (!reader.String(instruction.opcode) ||
                !reader.U32(operand_count)) {
                return false;
            }
            if (operand_count > kMaxInstructionOperands) {
                AddDiagnostic(diagnostics, "package_decode_limit",
                              "instruction operand count exceeds limit",
                              std::string(section));
                return false;
            }
            for (std::uint32_t operand_index = 0;
                 operand_index < operand_count; ++operand_index) {
                std::uint8_t type_code = 0;
                if (!reader.U8(type_code)) return false;
                const auto type = static_cast<OperandType>(type_code);
                InstructionOperand operand;
                operand.type = type;
                bool ok = false;
                switch (type) {
                case OperandType::U32:
                case OperandType::ResourceRef:
                case OperandType::ShaderRef:
                case OperandType::RootSignatureRef:
                    ok = reader.U32(operand.u32);
                    break;
                case OperandType::U64:
                    ok = reader.U64(operand.u64);
                    break;
                case OperandType::I64:
                    ok = reader.I64(operand.i64);
                    break;
                case OperandType::Bool:
                    ok = reader.Bool(operand.boolean);
                    break;
                case OperandType::String:
                    ok = reader.String(operand.string_value);
                    break;
                case OperandType::F32: {
                    std::uint32_t bits = 0;
                    ok = reader.U32(bits);
                    float value = 0.0f;
                    std::memcpy(&value, &bits, sizeof(value));
                    operand.f32 = value;
                    break;
                }
                default:
                    AddDiagnostic(diagnostics, "package_instruction_operand",
                                  "unknown instruction operand type",
                                  std::string(section));
                    return false;
                }
                if (!ok) return false;
                instruction.operands.push_back(std::move(operand));
            }
            output.push_back(std::move(instruction));
        }
        if (!reader.AtEnd()) {
            AddDiagnostic(diagnostics, "package_trailing_data",
                          "instruction section has trailing bytes",
                          std::string(section));
            return false;
        }
        return true;
    };
    if (!decode_instructions(kSectionInitialization, package.initialization)) {
        return false;
    }
    if (!decode_instructions(kSectionFrame, package.frame)) return false;
    // Decode PARM.
    {
        Reader reader{payloads[std::string(kSectionParameters)],
                      payload_sizes[std::string(kSectionParameters)], 0, &diagnostics,
                      std::string(kSectionParameters)};
        std::uint16_t version = 0;
        std::uint32_t count = 0;
        if (!reader.U16(version) || !reader.U32(count)) return false;
        if (version != 1u) {
            AddDiagnostic(diagnostics, "package_unknown_section_version",
                          "PARM section version is not supported",
                          std::string(kSectionParameters));
            return false;
        }
        if (count > kMaxRecordCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "PARM record count exceeds limit",
                          std::string(kSectionParameters));
            return false;
        }
        for (std::uint32_t i = 0; i < count; ++i) {
            ParameterRecord parameter;
            if (!reader.U32(parameter.parameter_id) ||
                !reader.String(parameter.name) ||
                !reader.String(parameter.type) ||
                !reader.String(parameter.phase) ||
                !reader.U32(parameter.component_count) ||
                !reader.Blob(parameter.value) ||
                !reader.U64(parameter.timeline_start_frame) ||
                !reader.U64(parameter.timeline_end_frame) ||
                !reader.U32(parameter.timeline_step) ||
                !reader.String(parameter.timeline_interpolation)) {
                return false;
            }
            package.parameters.push_back(std::move(parameter));
        }
        if (!reader.AtEnd()) {
            AddDiagnostic(diagnostics, "package_trailing_data",
                          "PARM section has trailing bytes",
                          std::string(kSectionParameters));
            return false;
        }
    }
    std::uint64_t decoded_bytes = 0u;
    const std::uint64_t decoded_limit =
        max_total_bytes > UINT64_MAX / 4u
            ? static_cast<std::uint64_t>(kMaxDecodedBytes)
            : std::min<std::uint64_t>(
                  kMaxDecodedBytes,
                  static_cast<std::uint64_t>(max_total_bytes) * 4u);
    auto charge_decoded = [&](std::uint64_t bytes,
                              std::string_view section) -> bool {
        if (decoded_bytes > decoded_limit ||
            bytes > decoded_limit - decoded_bytes) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "aggregate decoded payload exceeds the v1 memory budget",
                          std::string(section));
            return false;
        }
        decoded_bytes += bytes;
        return true;
    };
    const auto decode_root_signatures = [&]() {
        Reader reader{payloads[std::string(kSectionRootSignatures)],
                      payload_sizes[std::string(kSectionRootSignatures)], 0, &diagnostics,
                      std::string(kSectionRootSignatures)};
        std::uint16_t version = 0;
        std::uint32_t blob_count = 0;
        if (!reader.U16(version) || !reader.U32(blob_count)) return false;
        if (version != 1u) {
            AddDiagnostic(diagnostics, "package_unknown_section_version",
                          "RSIG section version is not supported",
                          std::string(kSectionRootSignatures));
            return false;
        }
        if (blob_count > kMaxRecordCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "RSIG blob count exceeds limit",
                          std::string(kSectionRootSignatures));
            return false;
        }
        std::vector<std::vector<std::uint8_t>> blobs;
        std::set<std::string> blob_digests;
        for (std::uint32_t i = 0; i < blob_count; ++i) {
            std::vector<std::uint8_t> blob;
            if (!reader.Blob(blob)) return false;
            if (!charge_decoded(blob.size(), kSectionRootSignatures)) {
                return false;
            }
            if (!blob_digests.insert(Sha256Hex(blob)).second) {
                AddDiagnostic(diagnostics,
                              "package_root_signature_not_deduplicated",
                              "RSIG blob table contains duplicates",
                              std::string(kSectionRootSignatures));
                return false;
            }
            blobs.push_back(std::move(blob));
        }
        std::uint32_t count = 0;
        if (!reader.U32(count)) return false;
        if (count > kMaxRecordCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "RSIG record count exceeds limit",
                          std::string(kSectionRootSignatures));
            return false;
        }
        for (std::uint32_t i = 0; i < count; ++i) {
            RootSignatureRecord record;
            std::uint32_t blob_index = 0;
            std::uint32_t binding_count = 0;
            if (!reader.U32(record.root_signature_id) ||
                !reader.String(record.family) ||
                !reader.String(record.convention) ||
                !reader.U32(record.dword_cost) ||
                !reader.U32(blob_index) || !reader.U32(binding_count)) {
                return false;
            }
            if (blob_index >= blobs.size()) {
                AddDiagnostic(diagnostics, "package_reference_out_of_range",
                              "RSIG record references a missing blob",
                              std::string(kSectionRootSignatures));
                return false;
            }
            if (!charge_decoded(blobs[blob_index].size(),
                                kSectionRootSignatures)) {
                return false;
            }
            if (binding_count > kMaxRecordCount) {
                AddDiagnostic(diagnostics, "package_decode_limit",
                              "RSIG binding count exceeds limit",
                              std::string(kSectionRootSignatures));
                return false;
            }
            record.blob = blobs[blob_index];
            for (std::uint32_t binding_index = 0;
                 binding_index < binding_count; ++binding_index) {
                RootBinding binding;
                if (!reader.String(binding.name) ||
                    !reader.String(binding.usage) ||
                    !reader.U32(binding.slot) || !reader.U32(binding.space) ||
                    !reader.U32(binding.root_parameter)) {
                    return false;
                }
                record.bindings.push_back(std::move(binding));
            }
            package.root_signatures.push_back(std::move(record));
        }
        if (!reader.AtEnd()) {
            AddDiagnostic(diagnostics, "package_trailing_data",
                          "RSIG section has trailing bytes",
                          std::string(kSectionRootSignatures));
            return false;
        }
        return true;
    };
    if (!decode_root_signatures()) return false;
    const auto decode_shaders = [&]() {
        Reader reader{payloads[std::string(kSectionShaders)], payload_sizes[std::string(kSectionShaders)],
                      0, &diagnostics, std::string(kSectionShaders)};
        std::uint16_t version = 0;
        std::uint32_t blob_count = 0;
        if (!reader.U16(version) || !reader.U32(blob_count)) return false;
        if (version != 1u) {
            AddDiagnostic(diagnostics, "package_unknown_section_version",
                          "SHDR section version is not supported",
                          std::string(kSectionShaders));
            return false;
        }
        if (blob_count > kMaxRecordCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "SHDR blob count exceeds limit",
                          std::string(kSectionShaders));
            return false;
        }
        std::vector<std::vector<std::uint8_t>> blobs;
        std::set<std::string> blob_digests;
        for (std::uint32_t i = 0; i < blob_count; ++i) {
            std::vector<std::uint8_t> blob;
            if (!reader.Blob(blob)) return false;
            if (!charge_decoded(blob.size(), kSectionShaders)) {
                return false;
            }
            if (!blob_digests.insert(Sha256Hex(blob)).second) {
                AddDiagnostic(diagnostics, "package_shader_blob_not_deduplicated",
                              "SHDR blob table contains duplicates",
                              std::string(kSectionShaders));
                return false;
            }
            blobs.push_back(std::move(blob));
        }
        std::uint32_t count = 0;
        if (!reader.U32(count)) return false;
        if (count > kMaxRecordCount) {
            AddDiagnostic(diagnostics, "package_decode_limit",
                          "SHDR record count exceeds limit",
                          std::string(kSectionShaders));
            return false;
        }
        for (std::uint32_t i = 0; i < count; ++i) {
            ShaderRecord shader;
            std::uint32_t blob_index = 0;
            if (!reader.U32(shader.shader_id) ||
                !reader.String(shader.entry_point) ||
                !reader.String(shader.target) || !reader.U32(blob_index) ||
                !reader.U32(shader.root_signature_id) ||
                !reader.U32(shader.flags) || !reader.String(shader.family) ||
                !reader.U32(shader.thread_group_x) ||
                !reader.U32(shader.thread_group_y) ||
                !reader.U32(shader.thread_group_z)) {
                return false;
            }
            if (blob_index >= blobs.size()) {
                AddDiagnostic(diagnostics, "package_reference_out_of_range",
                              "SHDR record references a missing blob",
                              std::string(kSectionShaders));
                return false;
            }
            if (!charge_decoded(blobs[blob_index].size(), kSectionShaders)) {
                return false;
            }
            shader.blob = blobs[blob_index];
            package.shaders.push_back(std::move(shader));
        }
        if (!reader.AtEnd()) {
            AddDiagnostic(diagnostics, "package_trailing_data",
                          "SHDR section has trailing bytes",
                          std::string(kSectionShaders));
            return false;
        }
        return true;
    };
    if (!decode_shaders()) return false;
    if (names.count(std::string(kSectionDev)) != 0u) {
        package.dev_metadata = std::vector<std::uint8_t>(
            payloads[std::string(kSectionDev)],
            payloads[std::string(kSectionDev)] + payload_sizes[std::string(kSectionDev)]);
    }
    std::map<std::string, std::uint32_t> actual_counts;
    actual_counts[std::string(kSectionCapability)] = 1u;
    actual_counts[std::string(kSectionResources)] =
        static_cast<std::uint32_t>(package.resources.size());
    actual_counts[std::string(kSectionInitialization)] =
        static_cast<std::uint32_t>(package.initialization.size());
    actual_counts[std::string(kSectionFrame)] =
        static_cast<std::uint32_t>(package.frame.size());
    actual_counts[std::string(kSectionParameters)] =
        static_cast<std::uint32_t>(package.parameters.size());
    actual_counts[std::string(kSectionRootSignatures)] =
        static_cast<std::uint32_t>(package.root_signatures.size());
    actual_counts[std::string(kSectionShaders)] =
        static_cast<std::uint32_t>(package.shaders.size());
    if (package.dev_metadata.has_value()) {
        actual_counts[std::string(kSectionDev)] = 0u;
    }
    for (const auto& entry : entries) {
        if (entry.record_count != actual_counts[entry.name]) {
            AddDiagnostic(diagnostics, "package_section_record_count",
                          "section '" + entry.name +
                              "' directory count does not match decoded count",
                          entry.name, entry.entry_offset, true);
            return false;
        }
    }
    package.flags = flags;
    return ValidatePackage(package, diagnostics, allow_dev);
}

std::vector<std::string> CheckReleaseLeakage(
    const std::vector<std::uint8_t>& package_bytes) {
    static const std::vector<std::string> kForbidden = {
        "nodewerk-studio", "fixtures/", "fixtures\\", "src/graph",
        "src\\graph", "nwgraph", "editor_layout", "source_map", "reflection",
        "DXIL debug", "#line", "expression:", "authoring"};
    std::vector<std::string> findings;
    std::string text(package_bytes.size(), '\0');
    if (!package_bytes.empty()) {
        std::memcpy(&text[0], package_bytes.data(), package_bytes.size());
    }
    for (const auto& needle : kForbidden) {
        if (text.find(needle) != std::string::npos) {
            findings.push_back("forbidden release string present: " + needle);
        }
    }
    Package package;
    std::vector<Diagnostic> diagnostics;
    if (!DecodePackage(package_bytes.data(), package_bytes.size(), package,
                       diagnostics, false)) {
        findings.push_back("package is not valid");
    } else if (package.dev_metadata.has_value()) {
        findings.push_back("release package contains DEVC metadata");
    } else if ((package.flags & 1u) == 0u) {
        findings.push_back("package is not marked as a release package");
    } else {
        for (const auto& shader : package.shaders) {
            std::string reason;
            if (HasForbiddenDxilMetadataChunks(shader.blob, reason)) {
                findings.push_back(
                    "shader " + std::to_string(shader.shader_id) + ": " +
                    reason);
            }
        }
    }
    return findings;
}

bool ValidateDxilPayload(const std::vector<std::uint8_t>& blob,
                         std::string& reason) {
    return ValidateDxilContainer(blob, reason);
}

std::string InspectJson(const Package& package, bool valid,
                        const std::vector<Diagnostic>& diagnostics,
                        std::size_t package_size_bytes,
                        std::string_view checksum_hex,
                        const std::vector<std::string>& leakage) {
    std::ostringstream out;
    out << "{\"valid\":" << (valid ? "true" : "false")
        << ",\"format\":\"nwp\",\"version\":{\"major\":1,\"minor\":0}"
        << ",\"size_bytes\":" << package_size_bytes
        << ",\"checksum\":{\"algorithm\":\"sha256\",\"digest\":"
        << JsonEscape(checksum_hex)
        << ",\"purpose\":\"corruption_detection_not_authenticity\"}"
        << ",\"diagnostics\":[";
    for (std::size_t i = 0; i < diagnostics.size(); ++i) {
        if (i != 0) out << ",";
        out << DiagnosticToJson(diagnostics[i]);
    }
    out << "]";
    if (valid) {
        out << ",\"capability\":{"
            << "\"profile_id\":" << JsonEscape(package.capability.profile_id)
            << ",\"requirements_digest\":"
            << JsonEscape(package.capability.requirements_digest)
            << ",\"profile_fingerprint\":"
            << JsonEscape(package.capability.profile_fingerprint)
            << ",\"compiler_fingerprint\":"
            << JsonEscape(package.capability.compiler_fingerprint)
            << ",\"feature_level\":" << package.capability.feature_level
            << ",\"feature_level_text\":\"12_0\""
            << ",\"feature_level_hex\":\"0xC000\""
            << ",\"shader_model\":" << package.capability.shader_model
            << ",\"shader_model_text\":"
            << JsonEscape(package.capability.shader_model_text)
            << ",\"shader_model_hex\":\"0x0060\""
            << ",\"resource_binding_tier\":"
            << package.capability.resource_binding_tier
            << ",\"resource_heap_tier\":"
            << package.capability.resource_heap_tier
            << ",\"binding_tier\":"
            << package.capability.resource_binding_tier
            << ",\"heap_tier\":"
            << package.capability.resource_heap_tier
            << ",\"runtime_dxc_dependency\":"
            << (package.capability.runtime_dxc_dependency ? "true" : "false")
            << ",\"runtime_slang_dependency\":"
            << (package.capability.runtime_slang_dependency ? "true" : "false")
            << ",\"required_dxgi_formats\":[";
        for (std::size_t i = 0; i < package.capability.required_dxgi_formats.size();
             ++i) {
            if (i != 0) out << ",";
            out << JsonEscape(package.capability.required_dxgi_formats[i]);
        }
        out << "],\"emitted_shader_targets\":[";
        for (std::size_t i = 0; i < package.capability.emitted_shader_targets.size();
             ++i) {
            if (i != 0) out << ",";
            out << JsonEscape(package.capability.emitted_shader_targets[i]);
        }
        out << "],\"dxgi_formats\":[";
        for (std::size_t i = 0; i < package.capability.required_dxgi_formats.size();
             ++i) {
            if (i != 0) out << ",";
            out << JsonEscape(package.capability.required_dxgi_formats[i]);
        }
        out << "],\"limits\":{"
            << "\"max_cbv_srv_uav_descriptors_per_frame\":"
            << package.capability.max_cbv_srv_uav_descriptors_per_frame
            << ",\"max_sampler_descriptors_per_frame\":"
            << package.capability.max_sampler_descriptors_per_frame
            << ",\"max_root_signature_dword_cost\":"
            << package.capability.max_root_signature_dword_cost
            << ",\"max_texture_2d_dimension\":"
            << package.capability.limit_max_texture_2d_dimension
            << ",\"max_compute_threads_per_group\":"
            << package.capability.limit_max_compute_threads_per_group
            << ",\"max_buffer_bytes\":"
            << package.capability.limit_max_buffer_bytes
            << ",\"swap_chain_buffer_count\":"
            << package.capability.limit_swap_chain_buffer_count
            << ",\"min_windows_build\":"
            << package.capability.min_windows_build << "}"
            << ",\"runtime_requirements\":{"
            << "\"direct_queue_count\":"
            << package.capability.direct_queue_count
            << ",\"runtime_dxc_dependency\":"
            << (package.capability.runtime_dxc_dependency ? "true" : "false")
            << ",\"runtime_slang_dependency\":"
            << (package.capability.runtime_slang_dependency ? "true" : "false")
            << ",\"agility_sdk_required\":"
            << (package.capability.agility_sdk_required ? "true" : "false")
            << ",\"toolchain_dxc_version\":"
            << JsonEscape(package.capability.toolchain_dxc_version)
            << ",\"toolchain_identity\":"
            << JsonEscape(package.capability.toolchain_identity) << "}"
            << ",\"optional_profile\":{"
            << "\"profile_id\":"
            << JsonEscape(package.capability.optional_profile_id)
            << ",\"requirements_digest\":"
            << JsonEscape(package.capability.optional_requirements_digest)
            << ",\"separate_artifact\":"
            << (package.capability.optional_profile_separate_artifact ? "true"
                                                                      : "false")
            << "}"
            << ",\"required_init_instructions\":[";
        for (std::size_t i = 0;
             i < package.capability.required_init_instructions.size(); ++i) {
            if (i != 0) out << ",";
            out << JsonEscape(package.capability.required_init_instructions[i]);
        }
        out << "],\"required_frame_instructions\":[";
        for (std::size_t i = 0;
             i < package.capability.required_frame_instructions.size(); ++i) {
            if (i != 0) out << ",";
            out << JsonEscape(package.capability.required_frame_instructions[i]);
        }
        out << "]}";
        out << ",\"resources\":[";
        for (std::size_t i = 0; i < package.resources.size(); ++i) {
            if (i != 0) out << ",";
            const auto& resource = package.resources[i];
            out << "{\"resource_id\":" << resource.resource_id
                << ",\"name\":" << JsonEscape(resource.name)
                << ",\"kind\":" << JsonEscape(resource.kind)
                << ",\"format\":" << JsonEscape(resource.format)
                << ",\"usage\":" << JsonEscape(resource.usage)
                << ",\"pass_family\":" << JsonEscape(resource.pass_family)
                << ",\"binding_slot\":" << resource.binding_slot
                << ",\"binding_space\":" << resource.binding_space
                << ",\"width\":" << resource.width
                << ",\"height\":" << resource.height
                << ",\"depth\":" << resource.depth
                << ",\"size_bytes\":" << resource.size_bytes << "}";
        }
        out << "]";
        out << ",\"initialization\":[";
        for (std::size_t i = 0; i < package.initialization.size(); ++i) {
            if (i != 0) out << ",";
            out << "{\"opcode\":"
                << JsonEscape(package.initialization[i].opcode) << "}";
        }
        out << "],\"frame\":[";
        for (std::size_t i = 0; i < package.frame.size(); ++i) {
            if (i != 0) out << ",";
            out << "{\"opcode\":" << JsonEscape(package.frame[i].opcode)
                << "}";
        }
        out << "],\"parameters\":[";
        for (std::size_t i = 0; i < package.parameters.size(); ++i) {
            if (i != 0) out << ",";
            const auto& parameter = package.parameters[i];
            out << "{\"parameter_id\":" << parameter.parameter_id
                << ",\"name\":" << JsonEscape(parameter.name)
                << ",\"type\":" << JsonEscape(parameter.type)
                << ",\"phase\":" << JsonEscape(parameter.phase)
                << ",\"component_count\":" << parameter.component_count
                << ",\"timeline_start_frame\":"
                << parameter.timeline_start_frame
                << ",\"timeline_end_frame\":"
                << parameter.timeline_end_frame
                << ",\"timeline_step\":" << parameter.timeline_step
                << ",\"timeline_interpolation\":"
                << JsonEscape(parameter.timeline_interpolation) << "}";
        }
        out << "]";
        out << ",\"shaders\":[";
        for (std::size_t i = 0; i < package.shaders.size(); ++i) {
            if (i != 0) out << ",";
            const auto& shader = package.shaders[i];
            out << "{\"shader_id\":" << shader.shader_id
                << ",\"entry_point\":" << JsonEscape(shader.entry_point)
                << ",\"target\":" << JsonEscape(shader.target)
                << ",\"blob_size\":" << shader.blob.size()
                << ",\"blob_sha256\":" << JsonEscape(Sha256Hex(shader.blob))
                << ",\"root_signature_id\":" << shader.root_signature_id
                << ",\"family\":" << JsonEscape(shader.family) << "}";
        }
        out << "]";
        out << ",\"root_signatures\":[";
        for (std::size_t i = 0; i < package.root_signatures.size(); ++i) {
            if (i != 0) out << ",";
            const auto& root = package.root_signatures[i];
            out << "{\"root_signature_id\":" << root.root_signature_id
                << ",\"family\":" << JsonEscape(root.family)
                << ",\"convention\":" << JsonEscape(root.convention)
                << ",\"dword_cost\":" << root.dword_cost
                << ",\"blob_size\":" << root.blob.size()
                << ",\"blob_sha256\":" << JsonEscape(Sha256Hex(root.blob))
                << "}";
        }
        out << "]";
        out << ",\"release\":{\"contains_authoring_metadata\":"
            << (package.dev_metadata.has_value() ? "true" : "false")
            << ",\"contains_source_paths\":false"
            << ",\"contains_source_text\":false"
            << ",\"contains_reflection_tables\":false}";
    }
    out << ",\"leak_scan\":{\"valid\":"
        << (leakage.empty() ? "true" : "false") << ",\"findings\":[";
    for (std::size_t i = 0; i < leakage.size(); ++i) {
        if (i != 0) out << ",";
        out << JsonEscape(leakage[i]);
    }
    out << "]}";
    out << "}";
    return out.str();
}

} // namespace nw::package
