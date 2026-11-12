#include "embedded_package.h"

#include "package/nwp_format.h"

#include <array>
#include <cstring>
#include <limits>
#include <string>
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
    bool ok = GetFileSizeEx(file, &size) != 0 && size.QuadPart > 0 &&
              size.QuadPart <=
                  static_cast<LONGLONG>(std::numeric_limits<std::size_t>::max());
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
    std::vector<std::uint8_t> image;
    if (!ReadCurrentImage(&image)) {
        if (package != nullptr) *package = EmbeddedPackage{};
        return false;
    }
    return ParseEmbeddedPackage(image.data(), image.size(), package);
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

} // namespace nw::player

