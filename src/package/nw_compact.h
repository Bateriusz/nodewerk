#pragma once

// NW-10 compact delivery container for the unchanged `.nwp` v1 package.
//
// The decoder reconstructs the exact canonical package bytes.  The established
// NW-03 decoder then performs all structural and semantic validation; the
// compact layer never bypasses package bounds checks, shader validation, or the
// release flag.

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace nw::package {

inline constexpr std::uint8_t kCompactMagic[8] = {
    'N', 'W', 'C', '1', '\n', 0, 0, 0};
inline constexpr std::uint16_t kCompactVersion = 1u;
inline constexpr std::uint16_t kCompactHeaderSize = 64u;
inline constexpr std::uint16_t kCompactCodecStored = 1u;
inline constexpr std::uint16_t kCompactCodecMszip = 2u;
inline constexpr std::uint16_t kCompactLayoutCanonical = 1u;
inline constexpr std::uint16_t kCompactLayoutAos = 2u;
inline constexpr std::uint16_t kCompactLayoutSoa = 3u;
inline constexpr std::size_t kCompactMaxContainerBytes = 64u * 1024u * 1024u;
inline constexpr std::size_t kCompactMaxDecodedBytes = 4u * 64u * 1024u * 1024u;
inline constexpr std::uint32_t kCompactMaxRecords = 1_048_576u;
inline constexpr std::uint32_t kCompactMaxExpansionRatio = 256u;

bool IsCompactPackage(const std::uint8_t* data, std::size_t size_bytes);

// Bounded reconstruction of the canonical `.nwp` bytes.  Every malformed,
// truncated, unsupported, reference-invalid, or expansion-limit violation
// fails before the caller can submit any GPU work.
bool DecodeCompactPackage(const std::uint8_t* data,
                          std::size_t size_bytes,
                          std::vector<std::uint8_t>* canonical,
                          std::string* error,
                          std::size_t max_decoded_bytes =
                              kCompactMaxDecodedBytes);

} // namespace nw::package
