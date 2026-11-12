#pragma once

// NW-05: self-contained generated-player package linkage.
//
// `nodewerkc export` appends the .nwp v1 package and generated link manifest to
// a specialized player executable using a deterministic footer.  The player
// locates that footer at startup; it never parses an authoring graph or
// reinterprets the package format.  Missing or corrupt embedded content is an
// initialization failure rather than a silent rendering fallback.

#include <cstddef>
#include <cstdint>
#include <vector>

namespace nw::player {

inline constexpr std::uint8_t kEmbeddedMagic[8] = {
    'N', 'W', 'E', 'M', 'B', '1', 0, 0};
inline constexpr std::uint32_t kEmbeddedVersion = 1u;
inline constexpr std::size_t kEmbeddedFooterSize = 92u;

struct EmbeddedPackage {
    // Owns the executable image read by LoadEmbeddedPackage.  The `bytes` and
    // `manifest` slices point into this buffer (or into caller-owned memory
    // passed to ParseEmbeddedPackage), so copies are intentionally disabled to
    // keep those pointers from outliving their backing storage.  A move
    // transfers the buffer and leaves the slices valid.
    std::vector<std::uint8_t> image;
    const std::uint8_t* bytes = nullptr;
    std::size_t size = 0u;
    const std::uint8_t* manifest = nullptr;
    std::size_t manifest_size = 0u;
    bool valid = false;

    EmbeddedPackage() = default;
    EmbeddedPackage(const EmbeddedPackage&) = delete;
    EmbeddedPackage& operator=(const EmbeddedPackage&) = delete;
    EmbeddedPackage(EmbeddedPackage&&) noexcept = default;
    EmbeddedPackage& operator=(EmbeddedPackage&&) noexcept = default;
};

// Reads the currently executing image and returns the embedded package.  On
// Windows this uses GetModuleFileNameW; host simulations can call
// ParseEmbeddedPackage with explicit bytes.
bool LoadEmbeddedPackage(EmbeddedPackage* package);

// Parses an explicit executable image.  Verifies the magic, version, lengths,
// and SHA-256 values before exposing slices.
bool ParseEmbeddedPackage(const std::uint8_t* executable,
                          std::size_t executable_size,
                          EmbeddedPackage* package);

// Reconstructs the embedded executable footer for verification tooling.
// Returns false when the supplied template/package cannot be embedded.
bool BuildEmbeddedImage(const std::uint8_t* template_bytes,
                        std::size_t template_size,
                        const std::uint8_t* package_bytes,
                        std::size_t package_size,
                        const std::uint8_t* manifest_bytes,
                        std::size_t manifest_size,
                        std::vector<std::uint8_t>* output);

} // namespace nw::player

