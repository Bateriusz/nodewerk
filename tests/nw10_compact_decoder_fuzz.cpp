// NW-10 shipped compact-decoder fuzz and parity harness.
//
// The harness consumes containers produced by the Python encoder so the C++
// player decoder and the contract encoder are exercised on identical bytes.
// It covers the stored and (on Windows) MSZIP codecs, malformed/truncated
// inputs, forward and out-of-range references, arithmetic limits, expansion
// limits, checksum mismatches, and deterministic mutation fuzzing.

#include "package/nw_compact.h"
#include "package/nwp_format.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

namespace {

using Bytes = std::vector<std::uint8_t>;

struct Failures {
    int count = 0;
    void Fail(const char* message) {
        std::fprintf(stderr, "nw10 decoder failure: %s\n", message);
        ++count;
    }
};

bool ReadFile(const char* path, Bytes* output) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) return false;
    output->assign(std::istreambuf_iterator<char>(stream),
                   std::istreambuf_iterator<char>());
    return stream.good() || stream.eof();
}

void WriteU16(Bytes* out, std::uint16_t value) {
    out->push_back(static_cast<std::uint8_t>(value & 0xFFu));
    out->push_back(static_cast<std::uint8_t>((value >> 8u) & 0xFFu));
}

void WriteU32(Bytes* out, std::uint32_t value) {
    for (std::uint32_t index = 0u; index < 4u; ++index) {
        out->push_back(static_cast<std::uint8_t>((value >> (index * 8u)) & 0xFFu));
    }
}

void Append(Bytes* out, const Bytes& value) {
    out->insert(out->end(), value.begin(), value.end());
}

Bytes Header(std::uint16_t codec, std::uint16_t layout, std::uint32_t flags,
             std::uint32_t decoded_size, std::uint32_t payload_size,
             std::uint32_t container_size, const Bytes& sha) {
    Bytes header;
    header.insert(header.end(), std::begin(nw::package::kCompactMagic),
                  std::end(nw::package::kCompactMagic));
    WriteU16(&header, nw::package::kCompactVersion);
    WriteU16(&header, nw::package::kCompactHeaderSize);
    WriteU16(&header, codec);
    WriteU16(&header, layout);
    WriteU32(&header, flags);
    WriteU32(&header, decoded_size);
    WriteU32(&header, payload_size);
    WriteU32(&header, container_size);
    Append(&header, sha);
    return header;
}

void AppendAosRecord(Bytes* table, std::uint8_t kind, std::uint32_t reference,
                     std::uint32_t length, std::uint32_t offset) {
    table->push_back(kind);
    table->push_back(0u);
    WriteU32(table, reference);
    WriteU32(table, length);
    WriteU32(table, offset);
}

Bytes StoredContainer(const Bytes& canonical, std::uint16_t layout,
                      bool forward_reference, bool short_reference,
                      bool oversized_count) {
    Bytes table;
    Bytes raw;
    const std::uint32_t count = oversized_count ? 2u : 3u;
    if (layout == nw::package::kCompactLayoutSoa) {
        Bytes kinds(count, 0u);
        Bytes refs;
        Bytes lengths;
        Bytes offsets;
        for (std::uint32_t index = 0u; index < count; ++index) {
            const std::size_t start =
                static_cast<std::size_t>(index) * canonical.size() / count;
            const std::size_t end =
                static_cast<std::size_t>(index + 1u) * canonical.size() / count;
            if (index == 1u) {
                kinds[index] = 1u;
                WriteU32(&refs, forward_reference ? 2u : 0u);
                WriteU32(&lengths, short_reference
                                         ? static_cast<std::uint32_t>(end - start) - 1u
                                         : static_cast<std::uint32_t>(end - start));
                WriteU32(&offsets, 0u);
            } else {
                kinds[index] = 0u;
                WriteU32(&refs, 0u);
                WriteU32(&lengths, static_cast<std::uint32_t>(end - start));
                WriteU32(&offsets, static_cast<std::uint32_t>(raw.size()));
                raw.insert(raw.end(), canonical.begin() + static_cast<std::ptrdiff_t>(start),
                           canonical.begin() + static_cast<std::ptrdiff_t>(end));
            }
        }
        Append(&table, kinds);
        Append(&table, refs);
        Append(&table, lengths);
        Append(&table, offsets);
    } else {
        for (std::uint32_t index = 0u; index < count; ++index) {
            const std::size_t start =
                static_cast<std::size_t>(index) * canonical.size() / count;
            const std::size_t end =
                static_cast<std::size_t>(index + 1u) * canonical.size() / count;
            if (index == 1u) {
                AppendAosRecord(&table, 1u, forward_reference ? 2u : 0u,
                                short_reference
                                    ? static_cast<std::uint32_t>(end - start) - 1u
                                    : static_cast<std::uint32_t>(end - start),
                                0u);
            } else {
                AppendAosRecord(&table, 0u, 0u,
                                static_cast<std::uint32_t>(end - start),
                                static_cast<std::uint32_t>(raw.size()));
                raw.insert(raw.end(), canonical.begin() + static_cast<std::ptrdiff_t>(start),
                           canonical.begin() + static_cast<std::ptrdiff_t>(end));
            }
        }
    }
    Bytes container;
    WriteU32(&container, count);
    Append(&container, table);
    Append(&container, raw);
    return container;
}

bool Decode(const Bytes& data, Bytes* canonical, std::string* error) {
    return nw::package::DecodeCompactPackage(
        data.data(), data.size(), canonical, error);
}

bool Same(const Bytes& left, const Bytes& right) {
    return left == right;
}

Bytes Sha(const Bytes& bytes) {
    const std::string hex = nw::package::Sha256Hex(bytes);
    Bytes digest;
    digest.reserve(32u);
    for (std::size_t index = 0u; index + 1u < hex.size(); index += 2u) {
        const auto nibble = [](char value) -> std::uint8_t {
            if (value >= '0' && value <= '9') {
                return static_cast<std::uint8_t>(value - '0');
            }
            return static_cast<std::uint8_t>(value - 'a' + 10);
        };
        digest.push_back(static_cast<std::uint8_t>(
            (nibble(hex[index]) << 4u) | nibble(hex[index + 1u])));
    }
    return digest;
}

void ExpectFailure(Failures* failures, const Bytes& data, const char* label) {
    Bytes decoded;
    std::string error;
    if (Decode(data, &decoded, &error)) {
        failures->Fail(label);
    }
}

void RunMalformedSuite(Failures* failures, const Bytes& canonical) {
    const std::uint32_t flags = 1u | 2u;
    for (std::uint16_t layout :
         {nw::package::kCompactLayoutAos, nw::package::kCompactLayoutSoa}) {
        Bytes container = StoredContainer(canonical, layout, false, false, false);
        Bytes sha = Sha(canonical);
        Bytes valid = Header(nw::package::kCompactCodecStored, layout, flags,
                             static_cast<std::uint32_t>(canonical.size()),
                             static_cast<std::uint32_t>(container.size()),
                             static_cast<std::uint32_t>(container.size()), sha);
        Append(&valid, container);
        Bytes decoded;
        std::string error;
        if (!Decode(valid, &decoded, &error) || !Same(decoded, canonical)) {
            failures->Fail("valid stored container did not round-trip");
        }

        for (std::size_t offset = 0u; offset < valid.size(); offset += 7u) {
            Bytes truncated(valid.begin(),
                            valid.begin() + static_cast<std::ptrdiff_t>(offset));
            ExpectFailure(failures, truncated, "truncated container accepted");
        }
        for (std::size_t header_size = 8u;
             header_size < nw::package::kCompactHeaderSize; ++header_size) {
            Bytes truncated(valid.begin(),
                            valid.begin() + static_cast<std::ptrdiff_t>(header_size));
            ExpectFailure(failures, truncated, "truncated header accepted");
        }

        Bytes bad = valid;
        bad[0] = 'X';
        ExpectFailure(failures, bad, "bad magic accepted");
        bad = valid;
        bad[8] = 2u;
        ExpectFailure(failures, bad, "unsupported version accepted");
        bad = valid;
        bad[10] = 63u;
        ExpectFailure(failures, bad, "unsupported header size accepted");
        bad = valid;
        bad[12] = 99u;
        ExpectFailure(failures, bad, "unsupported codec accepted");
        bad = valid;
        bad[14] = 99u;
        ExpectFailure(failures, bad, "unsupported layout accepted");
        bad = valid;
        bad[16] = 0x80u;
        ExpectFailure(failures, bad, "unsupported flags accepted");
        bad = valid;
        bad[24] = static_cast<std::uint8_t>(bad[24] + 1u);
        ExpectFailure(failures, bad, "payload size mismatch accepted");

        Bytes forward =
            StoredContainer(canonical, layout, true, false, false);
        Bytes forward_data = Header(
            nw::package::kCompactCodecStored, layout, flags,
            static_cast<std::uint32_t>(canonical.size()),
            static_cast<std::uint32_t>(forward.size()),
            static_cast<std::uint32_t>(forward.size()), sha);
        Append(&forward_data, forward);
        ExpectFailure(failures, forward_data, "forward reference accepted");

        Bytes short_ref =
            StoredContainer(canonical, layout, false, true, false);
        Bytes short_data = Header(
            nw::package::kCompactCodecStored, layout, flags,
            static_cast<std::uint32_t>(canonical.size()),
            static_cast<std::uint32_t>(short_ref.size()),
            static_cast<std::uint32_t>(short_ref.size()), sha);
        Append(&short_data, short_ref);
        ExpectFailure(failures, short_data, "mismatched reference length accepted");
    }

    Bytes oversized;
    WriteU32(&oversized, nw::package::kCompactMaxRecords + 1u);
    Bytes sha = Sha(canonical);
    Bytes data = Header(nw::package::kCompactCodecStored,
                        nw::package::kCompactLayoutAos, flags,
                        static_cast<std::uint32_t>(canonical.size()),
                        static_cast<std::uint32_t>(oversized.size()),
                        static_cast<std::uint32_t>(oversized.size()), sha);
    Append(&data, oversized);
    ExpectFailure(failures, data, "oversized record count accepted");

    Bytes valid = Header(nw::package::kCompactCodecStored,
                         nw::package::kCompactLayoutAos, flags,
                         static_cast<std::uint32_t>(canonical.size()), 8u, 8u, sha);
    Bytes zero = StoredContainer(canonical, nw::package::kCompactLayoutAos,
                                 false, false, false);
    Bytes valid_container = valid;
    Append(&valid_container, zero);
    valid_container[24] = static_cast<std::uint8_t>(zero.size());
    valid_container[28] = static_cast<std::uint8_t>(zero.size());
    valid_container[25] = static_cast<std::uint8_t>(zero.size() >> 8u);
    valid_container[29] = static_cast<std::uint8_t>(zero.size() >> 8u);
    Bytes tampered = valid_container;
    tampered[64] ^= 0xFFu;
    ExpectFailure(failures, tampered, "checksum mismatch accepted");

    Bytes expansion = Header(nw::package::kCompactCodecStored,
                             nw::package::kCompactLayoutAos, flags,
                             nw::package::kCompactMaxDecodedBytes,
                             8u, 8u, sha);
    ExpectFailure(failures, expansion, "expansion limit accepted");
}

void RunMutationFuzz(Failures* failures, const Bytes& canonical,
                     const Bytes& valid) {
    std::uint64_t state = 0x4E573130u;
    for (int iteration = 0; iteration < 2000; ++iteration) {
        Bytes candidate = valid;
        state = state * 6364136223846793005ULL + 1442695040888963407ULL;
        const int mutations = 1 + static_cast<int>((state >> 33u) % 4u);
        for (int index = 0; index < mutations; ++index) {
            state = state * 6364136223846793005ULL + 1442695040888963407ULL;
            const std::size_t position =
                static_cast<std::size_t>(state % candidate.size());
            candidate[position] ^= static_cast<std::uint8_t>(
                1u + ((state >> 17u) % 255u));
        }
        Bytes decoded;
        std::string error;
        if (Decode(candidate, &decoded, &error)) {
            if (!Same(decoded, canonical)) {
                failures->Fail("mutation produced a valid but different package");
            }
        }
    }
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 4) {
        std::fprintf(stderr,
                     "usage: %s <canonical.bin> <stored.nwc> <mszip.nwc>\n",
                     argv[0]);
        return 2;
    }
    Bytes canonical;
    Bytes stored;
    Bytes mszip;
    if (!ReadFile(argv[1], &canonical) || !ReadFile(argv[2], &stored) ||
        !ReadFile(argv[3], &mszip)) {
        std::fprintf(stderr, "failed to read decoder fixtures\n");
        return 2;
    }
    Failures failures;
    Bytes decoded;
    std::string error;
    if (!Decode(stored, &decoded, &error) || !Same(decoded, canonical)) {
        failures.Fail("python stored container did not decode identically");
    }
#if defined(_WIN32)
    decoded.clear();
    error.clear();
    if (!Decode(mszip, &decoded, &error) || !Same(decoded, canonical)) {
        failures.Fail("python MSZIP container did not decode identically");
    }
#endif
    RunMalformedSuite(&failures, canonical);
    RunMutationFuzz(&failures, canonical, stored);
    if (failures.count != 0) {
        std::fprintf(stderr, "%d decoder failures\n", failures.count);
        return 1;
    }
    std::printf("nw10 compact decoder fuzz/parity: PASS\n");
    return 0;
}
