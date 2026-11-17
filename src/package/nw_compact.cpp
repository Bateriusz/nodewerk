#include "nw_compact.h"

#include "nwp_format.h"

#include <algorithm>
#include <cstring>
#include <limits>
#include <string>
#include <utility>
#include <vector>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <compressapi.h>
#include <windows.h>
#pragma comment(lib, "cabinet.lib")
#endif

namespace nw::package {
namespace {

constexpr std::size_t kHeaderOffsetCodec = 12u;
constexpr std::size_t kHeaderOffsetLayout = 14u;
constexpr std::size_t kHeaderOffsetFlags = 16u;
constexpr std::size_t kHeaderOffsetDecodedSize = 20u;
constexpr std::size_t kHeaderOffsetPayloadSize = 24u;
constexpr std::size_t kHeaderOffsetContainerSize = 28u;
constexpr std::size_t kHeaderOffsetSha256 = 32u;
constexpr std::uint32_t kFlagDeltaReferences = 1u;
constexpr std::uint32_t kFlagRecordLayout = 2u;
constexpr std::uint32_t kKnownFlags = kFlagDeltaReferences | kFlagRecordLayout;
constexpr std::uint8_t kRecordRaw = 0u;
constexpr std::uint8_t kRecordReference = 1u;
constexpr std::size_t kRecordAosSize = 14u;
constexpr std::size_t kRecordSoaSize = 13u;

std::uint16_t ReadU16(const std::uint8_t* data) {
    return static_cast<std::uint16_t>(data[0]) |
           static_cast<std::uint16_t>(data[1]) << 8u;
}

std::uint32_t ReadU32(const std::uint8_t* data) {
    return static_cast<std::uint32_t>(data[0]) |
           (static_cast<std::uint32_t>(data[1]) << 8u) |
           (static_cast<std::uint32_t>(data[2]) << 16u) |
           (static_cast<std::uint32_t>(data[3]) << 24u);
}

bool CheckedMul(std::size_t left, std::size_t right, std::size_t limit,
                std::size_t* result) {
    if (result == nullptr) return false;
    if (left != 0u && right > limit / left) return false;
    *result = left * right;
    return true;
}

bool DecodeMszip(const std::uint8_t* payload, std::size_t payload_size,
                 std::size_t expected_size, std::vector<std::uint8_t>* output) {
#if defined(_WIN32)
    HANDLE decompressor = nullptr;
    if (!CreateDecompressor(COMPRESS_ALGORITHM_MSZIP, nullptr, &decompressor)) {
        return false;
    }
    output->assign(expected_size, 0u);
    SIZE_T written = 0u;
    const BOOL ok = Decompress(decompressor, payload,
                               static_cast<SIZE_T>(payload_size),
                               output->data(),
                               static_cast<SIZE_T>(output->size()), &written);
    CloseDecompressor(decompressor);
    return ok != 0 && written == static_cast<SIZE_T>(expected_size);
#else
    (void)payload;
    (void)payload_size;
    (void)expected_size;
    (void)output;
    return false;
#endif
}

void SetError(std::string* error, const char* message) {
    if (error != nullptr) *error = message;
}

std::string HexBytes(const std::uint8_t* data, std::size_t size) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string result;
    result.reserve(size * 2u);
    for (std::size_t index = 0u; index < size; ++index) {
        result.push_back(kHex[(data[index] >> 4u) & 0x0Fu]);
        result.push_back(kHex[data[index] & 0x0Fu]);
    }
    return result;
}

} // namespace

bool IsCompactPackage(const std::uint8_t* data, std::size_t size_bytes) {
    return data != nullptr && size_bytes >= kCompactHeaderSize &&
           std::memcmp(data, kCompactMagic, sizeof(kCompactMagic)) == 0;
}

bool DecodeCompactPackage(const std::uint8_t* data, std::size_t size_bytes,
                          std::vector<std::uint8_t>* canonical,
                          std::string* error,
                          std::size_t max_decoded_bytes) {
    if (canonical == nullptr) {
        SetError(error, "compact output target is null");
        return false;
    }
    canonical->clear();
    if (error != nullptr) error->clear();
    if (!IsCompactPackage(data, size_bytes)) {
        SetError(error, "compact_magic");
        return false;
    }
    const std::uint16_t version = ReadU16(data + 8u);
    const std::uint16_t header_size = ReadU16(data + 10u);
    const std::uint16_t codec = ReadU16(data + kHeaderOffsetCodec);
    const std::uint16_t layout = ReadU16(data + kHeaderOffsetLayout);
    const std::uint32_t flags = ReadU32(data + kHeaderOffsetFlags);
    const std::uint32_t decoded_size = ReadU32(data + kHeaderOffsetDecodedSize);
    const std::uint32_t payload_size = ReadU32(data + kHeaderOffsetPayloadSize);
    const std::uint32_t container_size =
        ReadU32(data + kHeaderOffsetContainerSize);
    if (version != kCompactVersion || header_size != kCompactHeaderSize) {
        SetError(error, "compact_header_unsupported");
        return false;
    }
    if (codec != kCompactCodecStored && codec != kCompactCodecMszip) {
        SetError(error, "compact_codec_unsupported");
        return false;
    }
    if (layout != kCompactLayoutCanonical && layout != kCompactLayoutAos &&
        layout != kCompactLayoutSoa) {
        SetError(error, "compact_layout_unsupported");
        return false;
    }
    if ((flags & ~kKnownFlags) != 0u) {
        SetError(error, "compact_flags_unsupported");
        return false;
    }
    if (payload_size != size_bytes - kCompactHeaderSize) {
        SetError(error, "compact_size_mismatch");
        return false;
    }
    if (decoded_size == 0u || decoded_size > max_decoded_bytes ||
        container_size > kCompactMaxContainerBytes ||
        container_size > max_decoded_bytes ||
        static_cast<std::uint64_t>(decoded_size) >
            static_cast<std::uint64_t>(container_size == 0u ? 1u : container_size) *
                kCompactMaxExpansionRatio) {
        SetError(error, "compact_decode_limit");
        return false;
    }
    const std::uint8_t* payload = data + kCompactHeaderSize;
    std::vector<std::uint8_t> container;
    if (codec == kCompactCodecStored) {
        if (payload_size != container_size) {
            SetError(error, "compact_size_mismatch");
            return false;
        }
        container.assign(payload, payload + payload_size);
    } else {
        if (!DecodeMszip(payload, payload_size, container_size, &container)) {
            SetError(error, "compact_codec_unavailable_or_malformed");
            return false;
        }
    }
    if (container.size() < 4u) {
        SetError(error, "compact_truncated");
        return false;
    }
    const std::uint32_t record_count = ReadU32(container.data());
    if (record_count > kCompactMaxRecords) {
        SetError(error, "compact_decode_limit");
        return false;
    }
    std::size_t table_size = 0u;
    const std::size_t entry_size =
        layout == kCompactLayoutSoa ? kRecordSoaSize : kRecordAosSize;
    if (!CheckedMul(record_count, entry_size, kCompactMaxContainerBytes,
                    &table_size) ||
        table_size > container.size() - 4u) {
        SetError(error, "compact_truncated");
        return false;
    }
    const std::uint8_t* table = container.data() + 4u;
    const std::size_t raw_start = 4u + table_size;
    const std::size_t raw_size = container.size() - raw_start;
    std::vector<std::uint8_t> record_kind(record_count, 0xFFu);
    std::vector<std::size_t> record_offset(record_count, 0u);
    std::vector<std::size_t> record_length(record_count, 0u);
    canonical->reserve(decoded_size);
    std::size_t expected_raw_offset = 0u;
    for (std::uint32_t index = 0u; index < record_count; ++index) {
        std::uint8_t kind = 0u;
        std::uint32_t reference = 0u;
        std::uint32_t length = 0u;
        std::uint32_t raw_offset = 0u;
        if (layout == kCompactLayoutSoa) {
            kind = table[index];
            const std::uint8_t* refs = table + record_count;
            const std::uint8_t* lengths = refs + record_count * 4u;
            const std::uint8_t* offsets = lengths + record_count * 4u;
            reference = ReadU32(refs + index * 4u);
            length = ReadU32(lengths + index * 4u);
            raw_offset = ReadU32(offsets + index * 4u);
        } else {
            const std::uint8_t* entry = table + index * kRecordAosSize;
            kind = entry[0];
            if (entry[1] != 0u) {
                SetError(error, "compact_malformed");
                return false;
            }
            reference = ReadU32(entry + 2u);
            length = ReadU32(entry + 6u);
            raw_offset = ReadU32(entry + 10u);
        }
        record_kind[index] = kind;
        if (kind == kRecordRaw) {
            if (length == 0u || raw_offset != expected_raw_offset ||
                static_cast<std::size_t>(length) >
                    raw_size - std::min<std::size_t>(raw_size, raw_offset)) {
                SetError(error, "compact_malformed");
                return false;
            }
            if (canonical->size() + length > decoded_size) {
                SetError(error, "compact_decode_limit");
                return false;
            }
            record_offset[index] = canonical->size();
            record_length[index] = length;
            canonical->insert(canonical->end(), container.data() + raw_start +
                                                    raw_offset,
                              container.data() + raw_start + raw_offset + length);
            expected_raw_offset += length;
        } else if (kind == kRecordReference) {
            if (reference >= index || raw_offset != 0u ||
                record_kind[reference] != kRecordRaw ||
                record_length[reference] != length) {
                SetError(error, "compact_reference_invalid");
                return false;
            }
            if (canonical->size() + length > decoded_size) {
                SetError(error, "compact_decode_limit");
                return false;
            }
            const std::size_t source_offset = record_offset[reference];
            const auto begin = canonical->begin() +
                               static_cast<std::ptrdiff_t>(source_offset);
            const auto end = begin + static_cast<std::ptrdiff_t>(length);
            canonical->insert(canonical->end(), begin, end);
            record_offset[index] = canonical->size() - length;
            record_length[index] = length;
        } else {
            SetError(error, "compact_malformed");
            return false;
        }
    }
    if (expected_raw_offset != raw_size || canonical->size() != decoded_size) {
        SetError(error, "compact_size_mismatch");
        return false;
    }
    const std::string expected_hex = HexBytes(data + kHeaderOffsetSha256, 32u);
    const std::string actual_hex = Sha256Hex(canonical->data(), canonical->size());
    if (expected_hex.size() != 64u || actual_hex != expected_hex) {
        SetError(error, "compact_checksum_mismatch");
        canonical->clear();
        return false;
    }
    return true;
}

} // namespace nw::package
