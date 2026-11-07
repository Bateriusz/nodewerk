// Source-tree placeholder only. CMake does NOT place this directory on the include path:
// the offline DXC target (nw_offline_shaders) must generate
// ${CMAKE_BINARY_DIR}/shaders/generated/compute_fixture_dxil.h and that build directory is
// the only include root. A non-CMake compile that uses this stub still fails at runtime in
// ValidateOfflineDxilContainer with ExitCode::OfflineShaderPayloadInvalid (40), never
// silently falling back to runtime shader compilation.
const unsigned char kComputeFixtureDxil[] = {
    0x00, 0x00, 0x00, 0x00
};
