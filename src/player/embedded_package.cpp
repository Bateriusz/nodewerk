#include "embedded_package.h"

#include "package/nwp_format.h"

#include <array>
#include <cstring>
#include <limits>
#include <string>
#include <utility>
#include <vector>

#if defined(_WIN32)
#include <windows.h>
#else
#include <cstdio>
#endif

namespace nw::player {
namespace {

std::uint32_t ReadU32(const std::uint8_t* data) {
    return static_cast<std::uint32_t>(data[0]) |
           (static_cast<std::uint32_t>(data[1]) << 8u) |
           (static_cast<std::uint32_t>(data[2]) << 16u) |
           (static_cast<std::uint32_t>(data[3]) << 24u);
}

std::uint64_t ReadU64(const std::uint8_t* data) {
    std::uint64_t value = 0u;
    for (std::uint32_t index = 0u; index < 8u; ++index) {
        value |= static_cast<std::uint64_t>(data[index]) << (index * 8u);
    }
    return value;
}

void WriteU32(std::vector<std::uint8_t>* output, std::uint32_t value) {
    for (std::uint32_t index = 0u; index < 4u; ++index) {
        output->push_back(static_cast<std::uint8_t>((value >> (index * 8u)) & 0xFFu));
    }
}

void WriteU64(std::vector<std::uint8_t>* output, std::uint64_t value) {
    for (std::uint32_t index = 0u; index < 8u; ++index) {
        output->push_back(static_cast<std::uint8_t>((value >> (index * 8u)) & 0xFFu));
    }
}

bool Sha256Matches(const std::uint8_t* data, std::size_t size,
                   const std::uint8_t* expected) {
    const std::string hex =
        package::Sha256Hex(data, size);
    if (hex.size() != 64u) return false;
    static constexpr char kHex[] = "0123456789abcdef";
    for (std::size_t index = 0u; index < 32u; ++index) {
        const std::uint8_t high =
            static_cast<std::uint8_t>(kHex[(expected[index] >> 4u) & 0xFu]);
        const std::uint8_t low =
            static_cast<std::uint8_t>(kHex[expected[index] & 0xFu]);
        // The package helper returns lowercase hex bytes.  Compare the two
        // nibbles of each byte in order.
        if (hex[index * 2u] != static_cast<char>(high) ||
            hex[index * 2u + 1u] != static_cast<char>(low)) {
            return false;
        }
    }
    return true;
}

bool ReadCurrentImage(std::vector<std::uint8_t>* bytes) {
#if defined(_WIN32)
    std::wstring path(32768u, L'\0');
    const DWORD length = GetModuleFileNameW(
        nullptr, path.data(), static_cast<DWORD>(path.size()));
    if (length == 0u || length >= path.size()) return false;
    path.resize(length);
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                              nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                              nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size{};
    // File sizes are signed in LARGE_INTEGER, but size_t is unsigned on
    // Windows x64. Casting SIZE_MAX to LONGLONG yields -1 and would reject
    // every non-empty executable. Also bound the single ReadFile call below
    // to DWORD_MAX so its length cannot truncate.
    bool ok = GetFileSizeEx(file, &size) != 0 && size.QuadPart > 0 &&
              static_cast<std::uint64_t>(size.QuadPart) <=
                  static_cast<std::uint64_t>(std::numeric_limits<DWORD>::max());
    if (ok) {
        bytes->resize(static_cast<std::size_t>(size.QuadPart));
        DWORD read = 0u;
        ok = ReadFile(file, bytes->data(), static_cast<DWORD>(bytes->size()),
                      &read, nullptr) != 0 &&
             read == static_cast<DWORD>(bytes->size());
    }
    CloseHandle(file);
    return ok;
#else
    std::FILE* file = std::fopen("/proc/self/exe", "rb");
    if (file == nullptr) return false;
    bool ok = std::fseek(file, 0, SEEK_END) == 0;
    const long length = ok ? std::ftell(file) : -1;
    ok = ok && length > 0;
    if (ok) {
        bytes->resize(static_cast<std::size_t>(length));
        ok = std::fseek(file, 0, SEEK_SET) == 0 &&
             std::fread(bytes->data(), 1u, bytes->size(), file) == bytes->size();
    }
    std::fclose(file);
    return ok;
#endif
}

} // namespace

bool ParseEmbeddedPackage(const std::uint8_t* executable,
                          std::size_t executable_size,
                          EmbeddedPackage* package) {
    if (package == nullptr) return false;
    *package = EmbeddedPackage{};
    if (executable == nullptr || executable_size < kEmbeddedFooterSize) {
        return false;
    }
    const std::uint8_t* footer =
        executable + executable_size - kEmbeddedFooterSize;
    if (std::memcmp(footer, kEmbeddedMagic, sizeof(kEmbeddedMagic)) != 0) {
        return false;
    }
    const std::uint32_t version = ReadU32(footer + 8u);
    if (version != kEmbeddedVersion) return false;
    const std::uint64_t package_size = ReadU64(footer + 12u);
    const std::uint64_t manifest_size = ReadU64(footer + 20u);
    if (package_size == 0u ||
        package_size > executable_size ||
        manifest_size > executable_size ||
        package_size + manifest_size + kEmbeddedFooterSize > executable_size) {
        return false;
    }
    const std::size_t start = executable_size -
        static_cast<std::size_t>(package_size + manifest_size + kEmbeddedFooterSize);
    const std::uint8_t* package_bytes = executable + start;
    const std::uint8_t* manifest_bytes = package_bytes + package_size;
    if (!Sha256Matches(package_bytes, static_cast<std::size_t>(package_size),
                       footer + 28u) ||
        !Sha256Matches(manifest_bytes, static_cast<std::size_t>(manifest_size),
                       footer + 60u)) {
        return false;
    }
    package->bytes = package_bytes;
    package->size = static_cast<std::size_t>(package_size);
    package->manifest = manifest_bytes;
    package->manifest_size = static_cast<std::size_t>(manifest_size);
    package->valid = true;
    return true;
}

bool LoadEmbeddedPackage(EmbeddedPackage* package) {
    if (package == nullptr) return false;
    *package = EmbeddedPackage{};
    if (!ReadCurrentImage(&package->image)) {
        *package = EmbeddedPackage{};
        return false;
    }
    EmbeddedPackage parsed{};
    if (!ParseEmbeddedPackage(package->image.data(), package->image.size(),
                              &parsed)) {
        *package = EmbeddedPackage{};
        return false;
    }
    const std::uint8_t* base = package->image.data();
    const std::size_t package_offset =
        static_cast<std::size_t>(parsed.bytes - base);
    const std::size_t manifest_offset =
        static_cast<std::size_t>(parsed.manifest - base);
    package->bytes = base + package_offset;
    package->size = parsed.size;
    package->manifest = base + manifest_offset;
    package->manifest_size = parsed.manifest_size;
    package->valid = true;
    return true;
}

bool BuildEmbeddedImage(const std::uint8_t* template_bytes,
                        std::size_t template_size,
                        const std::uint8_t* package_bytes,
                        std::size_t package_size,
                        const std::uint8_t* manifest_bytes,
                        std::size_t manifest_size,
                        std::vector<std::uint8_t>* output) {
    if (output == nullptr || template_bytes == nullptr || package_bytes == nullptr ||
        manifest_bytes == nullptr || package_size == 0u) {
        return false;
    }
    const std::string package_hex = package::Sha256Hex(package_bytes, package_size);
    const std::string manifest_hex =
        package::Sha256Hex(manifest_bytes, manifest_size);
    if (package_hex.size() != 64u || manifest_hex.size() != 64u) return false;
    output->assign(template_bytes, template_bytes + template_size);
    output->insert(output->end(), package_bytes, package_bytes + package_size);
    output->insert(output->end(), manifest_bytes, manifest_bytes + manifest_size);
    output->insert(output->end(), kEmbeddedMagic,
                   kEmbeddedMagic + sizeof(kEmbeddedMagic));
    WriteU32(output, kEmbeddedVersion);
    WriteU64(output, static_cast<std::uint64_t>(package_size));
    WriteU64(output, static_cast<std::uint64_t>(manifest_size));
    auto append_hex = [output](const std::string& hex) {
        for (std::size_t index = 0u; index + 1u < hex.size(); index += 2u) {
            const auto nibble = [](char value) -> std::uint8_t {
                if (value >= '0' && value <= '9') {
                    return static_cast<std::uint8_t>(value - '0');
                }
                if (value >= 'a' && value <= 'f') {
                    return static_cast<std::uint8_t>(value - 'a' + 10);
                }
                if (value >= 'A' && value <= 'F') {
                    return static_cast<std::uint8_t>(value - 'A' + 10);
                }
                return 0u;
            };
            output->push_back(
                static_cast<std::uint8_t>((nibble(hex[index]) << 4u) |
                                          nibble(hex[index + 1u])));
        }
    };
    append_hex(package_hex);
    append_hex(manifest_hex);
    return output->size() ==
           template_size + package_size + manifest_size + kEmbeddedFooterSize;
}

bool ParseEmbeddedLinkMetadata(const EmbeddedPackage& package,
                              EmbeddedLinkMetadata* metadata) {
    if (metadata == nullptr) return false;
    *metadata = EmbeddedLinkMetadata{};
    if (!package.valid || package.manifest == nullptr ||
        package.manifest_size == 0u) {
        return false;
    }
    const char* begin = reinterpret_cast<const char*>(package.manifest);
    const std::size_t size = package.manifest_size;
    const std::string text(begin, begin + size);
    const std::size_t metadata_offset = text.find("\"link_metadata\"");
    if (metadata_offset == std::string::npos) return false;
    const std::size_t object_begin = text.find('{', metadata_offset);
    if (object_begin == std::string::npos) return false;
    int depth = 0;
    std::size_t object_end = std::string::npos;
    for (std::size_t index = object_begin; index < text.size(); ++index) {
        if (text[index] == '{') ++depth;
        if (text[index] == '}') {
            --depth;
            if (depth == 0) {
                object_end = index;
                break;
            }
        }
    }
    if (object_end == std::string::npos) return false;
    const std::string object =
        text.substr(object_begin, object_end - object_begin + 1u);

    auto skip_space = [](const std::string& value,
                         std::size_t* cursor) -> bool {
        while (*cursor < value.size()) {
            const char ch = value[*cursor];
            if (ch != ' ' && ch != '\t' && ch != '\r' && ch != '\n') break;
            ++*cursor;
        }
        return *cursor < value.size();
    };
    auto delimiter = [](const std::string& value, std::size_t cursor) {
        return cursor >= value.size() || value[cursor] == ',' ||
               value[cursor] == '}' || value[cursor] == ' ' ||
               value[cursor] == '\t' || value[cursor] == '\r' ||
               value[cursor] == '\n';
    };
    auto find_value = [&object, &skip_space](const char* key) {
        const std::size_t key_offset = object.find(
            std::string("\"") + key + "\"");
        if (key_offset == std::string::npos) {
            return std::pair<bool, std::size_t>{false, 0u};
        }
        std::size_t cursor = object.find(':', key_offset);
        if (cursor == std::string::npos) {
            return std::pair<bool, std::size_t>{false, 0u};
        }
        ++cursor;
        if (!skip_space(object, &cursor)) {
            return std::pair<bool, std::size_t>{false, 0u};
        }
        return std::pair<bool, std::size_t>{true, cursor};
    };
    auto read_bool = [&object, &find_value, &delimiter](
                         const char* key, bool* output) -> bool {
        const auto located = find_value(key);
        if (!located.first) return false;
        if (object.compare(located.second, 4u, "true") == 0 &&
            delimiter(object, located.second + 4u)) {
            *output = true;
            return true;
        }
        if (object.compare(located.second, 5u, "false") == 0 &&
            delimiter(object, located.second + 5u)) {
            *output = false;
            return true;
        }
        return false;
    };
    const auto budget_location = find_value("memory_budget_bytes");
    if (!budget_location.first) return false;
    std::size_t cursor = budget_location.second;
    std::uint64_t budget = 0u;
    bool saw_digit = false;
    while (cursor < object.size() && object[cursor] >= '0' &&
           object[cursor] <= '9') {
        saw_digit = true;
        const std::uint64_t digit =
            static_cast<std::uint64_t>(object[cursor] - '0');
        if (budget > (std::numeric_limits<std::uint64_t>::max() - digit) /
                         10u) {
            return false;
        }
        budget = budget * 10u + digit;
        ++cursor;
    }
    if (!saw_digit || !delimiter(object, cursor)) return false;
    if (!read_bool("wave_ops", &metadata->wave_ops) ||
        !read_bool("fp16_shader_ops", &metadata->fp16_shader_ops) ||
        !read_bool("int64_shader_ops", &metadata->int64_shader_ops)) {
        return false;
    }
    metadata->memory_budget_bytes = budget;
    metadata->valid = true;
    return true;
}

} // namespace nw::player

